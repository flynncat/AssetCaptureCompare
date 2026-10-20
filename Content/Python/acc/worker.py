"""Slate-tick capture worker. This module must only run in a child editor."""
import os
import re
import time
import traceback
from pathlib import Path
import unreal
from .common import digest, file_stamps, read_json, utcnow, write_json
from . import geometry
from .timings import Timings

_worker = None


def vector(value):
    return [float(value.x), float(value.y), float(value.z)]


def apply_capture_mobility(actor, movable=True):
    components = [component for child in [actor] + list(actor.get_all_child_actors(True))
                  for component in child.get_components_by_class(unreal.SceneComponent)]
    def needs_movable(component):
        # UE4 skeletal components cannot have Static mobility, nor can their attached children.
        parent = component
        while parent:
            if isinstance(parent, unreal.SkeletalMeshComponent):
                return True
            parent = parent.get_attach_parent()
        return False
    expected = [(c, unreal.ComponentMobility.MOVABLE if movable or needs_movable(c) else unreal.ComponentMobility.STATIC)
                for c in components]
    changed = sum(c.get_editor_property("mobility") != value for c, value in expected)
    for component, value in expected:
        if component.get_editor_property("mobility") != value:
            component.set_mobility(value)
    if any(c.get_editor_property("mobility") != value for c, value in expected):
        raise RuntimeError("Capture component mobility does not match the requested mode")
    return {"policy": "movable" if movable else "static", "scene_components": len(components),
            "changed_components": changed, "verified": True,
            "movable_components": sum(value == unreal.ComponentMobility.MOVABLE for _, value in expected),
            "static_components": sum(value == unreal.ComponentMobility.STATIC for _, value in expected),
            "static_unsupported_components": sum(value == unreal.ComponentMobility.MOVABLE for _, value in expected) if not movable else 0}


def make_movable(actor):
    return apply_capture_mobility(actor, True)


def configure_capture_lighting(world, movable):
    lights = []
    for owner in unreal.AssetCaptureCompareLibrary.get_capture_actors(world):
        for sky in owner.get_components_by_class(unreal.SkyLightComponent):
            before = sky.get_editor_property("mobility")
            distance_before = sky.get_editor_property("sky_distance_threshold")
            # Static skylights only contribute baked lighting, unavailable to newly spawned targets.
            if movable and before != unreal.ComponentMobility.MOVABLE:
                sky.set_mobility(unreal.ComponentMobility.MOVABLE)
            # A baked sky's distant-only capture can be black in the mobile preview path.
            # Capture the environment instead; targets are hidden during resource preparation.
            if movable and before == unreal.ComponentMobility.STATIC and sky.get_editor_property("source_type") == unreal.SkyLightSourceType.SLS_CAPTURED_SCENE:
                sky.set_editor_property("sky_distance_threshold", 0.0)
            after = sky.get_editor_property("mobility")
            if after != unreal.ComponentMobility.STATIC:
                sky.recapture_sky()
            lights.append({"actor": owner.get_actor_label(), "before": str(before), "after": str(after),
                           "sky_distance_before": distance_before,
                           "sky_distance_after": sky.get_editor_property("sky_distance_threshold")})
    return {"policy": "dynamic_skylight" if movable else "scene_lighting", "skylights": lights}


def prepare_capture_resources(world, actor):
    targets = [actor] + list(actor.get_all_child_actors(True))
    hidden = [(target, target.get_editor_property("hidden")) for target in targets]
    try:
        for target, _ in hidden:
            target.set_actor_hidden_in_game(True)
        return unreal.AssetCaptureCompareLibrary.prepare_resources(world)
    finally:
        for target, value in hidden:
            target.set_actor_hidden_in_game(value)


def bounds(actor):
    actors = [actor] + list(actor.get_all_child_actors(True))
    boxes = []
    for child in actors:
        if child.get_editor_property("hidden"):
            continue
        for component in child.get_components_by_class(unreal.MeshComponent):
            if not component.is_visible() or component.get_editor_property("hidden_in_game"):
                continue
            center, extent, radius = unreal.SystemLibrary.get_component_bounds(component)
            if max(vector(extent)) > 0:
                boxes.append((vector(center), vector(extent)))
    if not boxes:
        raise ValueError("No visible Mesh components after editor construction; runtime-only Blueprints are unsupported")
    low = [min(c[i] - e[i] for c, e in boxes) for i in range(3)]
    high = [max(c[i] + e[i] for c, e in boxes) for i in range(3)]
    return [(a + b) / 2 for a, b in zip(low, high)], [(b - a) / 2 for a, b in zip(low, high)]


class Worker:
    def __init__(self, path):
        self.path = path
        self.config = read_json(path)
        self.generator = None
        self.task = None
        self.last_heartbeat = 0
        self.in_tick = False
        settings = unreal.load_class(None, "/Script/PythonScriptPlugin.PythonScriptPluginUserSettings")
        unreal.get_default_object(settings).set_editor_property("bDeveloperMode", False)
        performance = unreal.load_class(None, "/Script/UnrealEd.EditorPerformanceSettings")
        unreal.get_default_object(performance).set_editor_property("bThrottleCPUWhenNotForeground", False)
        self.handle = unreal.register_slate_post_tick_callback(self.tick)

    def heartbeat(self, phase):
        write_json(self.path.with_suffix(".status.json"), {"pid": os.getpid(), "phase": phase,
            "heartbeat": time.time(), "task": self.task.get("capture_id") if self.task else None})

    def tick(self, delta):
        if self.in_tick:
            return
        self.in_tick = True
        try:
            self.advance(delta)
        finally:
            self.in_tick = False

    def advance(self, delta):
        try:
            if self.path.with_suffix(".stop").exists() and self.generator is None:
                unreal.unregister_slate_post_tick_callback(self.handle)
                unreal.SystemLibrary.quit_editor()
                return
            if self.generator is None:
                inbox = self.path.with_suffix(".task.json")
                if inbox.exists():
                    self.task = read_json(inbox)
                    inbox.unlink()
                    self.generator = self.capture(self.task)
            if self.generator is not None:
                try:
                    phase = next(self.generator)
                    self.heartbeat(phase)
                except StopIteration:
                    self.generator = None
                    self.task = None
                    self.heartbeat("idle")
            elif time.monotonic() - self.last_heartbeat > 1:
                self.heartbeat("idle")
                self.last_heartbeat = time.monotonic()
        except Exception as error:
            if self.task:
                write_json(self.path.with_suffix(".result.json"), {"status": "failed", "error": str(error),
                    "traceback": traceback.format_exc(), "capture_id": self.task["capture_id"], "finished_at": utcnow()})
            unreal.log_error("[AssetCaptureCompare] " + traceback.format_exc())
            self.generator = None
            self.task = None

    def capture(self, task):
        profile = task["profile"]
        directory = Path(task["staging"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "views").mkdir(exist_ok=True)
        started = time.monotonic()
        timings = Timings()
        source_before = file_stamps(task["files"])
        if source_before != task["source_stamps"]:
            raise ValueError("Source changed after preflight; start a new batch")
        actor = capture_actor = render_target = None
        try:
            yield "loading_world"
            native = unreal.AssetCaptureCompareLibrary
            scene_file = Path(self.config["scene_file"])
            package = "/AssetCaptureCompare/" + scene_file.relative_to(Path(__file__).resolve().parents[3] / "Content").with_suffix("").as_posix()
            with timings.measure("world_load"):
                world = native.create_capture_world(package + "." + scene_file.stem)
            if not world:
                raise RuntimeError("Could not load the frozen capture map")
            native.freeze_capture_time(world, 1.0)
            lighting = configure_capture_lighting(world, profile.get("movable", True))
            if not getattr(self, "render_configured", False):
                for command in ("t.IdleWhenNotForeground 0", "r.EnableUseLessCPU 0", "t.MaxFPS 60", "r.VSync 0", "r.MotionBlurQuality 0", "r.DefaultFeature.AntiAliasing 1", "r.PostProcessAAQuality 2",
                                "r.Streaming.FullyLoadUsedTextures 1", "r.Streaming.UseAllMips 1", "r.ForceLOD 0"):
                    unreal.SystemLibrary.execute_console_command(world, command)
                self.render_configured = True
            yield "loading_asset"
            with timings.measure("asset_load"):
                source = unreal.load_asset(task["path"])
            if not source:
                raise ValueError("Asset failed to load")
            with timings.measure("actor_spawn"):
                actor = native.spawn_capture_asset(world, source)
            if not actor:
                raise ValueError("Could not create an editor preview actor")
            mobility = make_movable(actor)
            actor.set_actor_rotation(unreal.Rotator(0, profile["orientation"], 0), False)
            if not profile.get("movable", True):
                mobility = apply_capture_mobility(actor, False)
            for child in [actor] + list(actor.get_all_child_actors(True)):
                child.set_actor_tick_enabled(False)
                for component in child.get_components_by_class(unreal.ActorComponent):
                    component.set_component_tick_enabled(False)
                for component in child.get_components_by_class(unreal.PrimitiveComponent):
                    component.set_simulate_physics(False)
                for component in child.get_components_by_class(unreal.StaticMeshComponent):
                    component.set_forced_lod_model(int(profile["lod"]) + 1)
                for component in child.get_components_by_class(unreal.SkeletalMeshComponent):
                    component.set_forced_lod(int(profile["lod"]) + 1)
                    component.set_editor_property("pause_anims", True)
            yield "settling_bounds"
            if native:
                yield "waiting_resources"
                with timings.measure("resources"):
                    if not prepare_capture_resources(world, actor):
                        raise RuntimeError("Shaders or streamed resources are not ready")
            center, extent = bounds(actor)
            baseline = task.get("baseline") or {}
            framing = baseline.get("framing")
            reused = bool(framing and not profile["reframe"] and geometry.fits(center, extent, framing, profile))
            if not reused:
                framing = geometry.frame(center, extent, profile)
            framing_id = baseline["framing_id"] if reused else digest({"framing": framing, "capture_id": task["capture_id"]})
            capture_actor = native.spawn_capture_camera(world)
            component = capture_actor.get_component_by_class(unreal.SceneCaptureComponent2D)
            render_target = unreal.RenderingLibrary.create_render_target2d(world, profile["size"], profile["size"], unreal.TextureRenderTargetFormat.RTF_RGBA8)
            component.set_editor_property("texture_target", render_target)
            component.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
            component.set_editor_property("capture_every_frame", False)
            component.set_editor_property("capture_on_movement", False)
            component.set_editor_property("fov_angle", profile["fov"])
            component.set_editor_property("bOverride_CustomNearClippingPlane", True)
            component.set_editor_property("custom_near_clipping_plane", framing.get("near_cm", 10.0))
            # Static snapshots must not inherit temporal history from previous captures.
            component.set_editor_property("always_persist_rendering_state", False)
            post = component.get_editor_property("post_process_settings")
            for name, value in (("override_auto_exposure_min_brightness", True), ("override_auto_exposure_max_brightness", True),
                                ("auto_exposure_min_brightness", profile["exposure"]), ("auto_exposure_max_brightness", profile["exposure"]),
                                ("override_motion_blur_amount", True), ("motion_blur_amount", 0.0)):
                post.set_editor_property(name, value)
            component.set_editor_property("post_process_settings", post)
            if profile["hide_floor"]:
                component.set_editor_property("hidden_actors", [a for a in native.get_capture_actors(world) if a.get_actor_label() == "Floor"])
            result = {"schema": 1, "capture_id": task["capture_id"], "identity": task["identity"], "path": task["path"],
                      "class": task["class"], "started_at": utcnow(), "status": "success", "views": [], "worker_pid": os.getpid(),
                      "bounds": {"center": center, "extent": extent}, "framing": framing, "framing_id": framing_id,
                      "framing_reused": reused, "compatibility": task["compatibility"], "source_stamps": source_before,
                      "source_version": task["source_version"], "baseline_id": task.get("baseline_id"),
                      "capture_mobility": mobility, "capture_lighting": lighting}
            result["compatibility_details"] = task.get("compatibility_details", {})
            result["render_environment"] = native.get_render_environment() if native else "unverified"
            result["resources_verified"] = bool(native)
            result["placement"] = {"location": vector(actor.get_actor_location()), "scale": vector(actor.get_actor_scale3d()),
                                   "orientation_yaw": profile["orientation"]}
            for view in profile["views"]:
                outward, _, _ = geometry.basis(view)
                position = [framing["center"][i] + framing["distance"] * outward[i] for i in range(3)]
                location, target = unreal.Vector(*position), unreal.Vector(*framing["center"])
                rotation = unreal.MathLibrary.find_look_at_rotation(location, target)
                capture_actor.set_actor_location_and_rotation(location, rotation, False, True)
                component.set_editor_property("camera_cut_this_frame", True)
                with timings.measure("render_" + view["id"]):
                    for _ in range(profile["warmup_frames"]):
                        if native:
                            native.freeze_capture_time(world, 1.0)
                        component.capture_scene()
                        yield "warming_" + view["id"]
                    if native:
                        native.freeze_capture_time(world, 1.0)
                    component.capture_scene()
                    yield "capturing_" + view["id"]
                name = view["id"] + ".png"
                with timings.measure("export_" + view["id"]):
                    unreal.RenderingLibrary.export_render_target(world, render_target, str(directory / "views"), name)
                if not (directory / "views" / name).is_file() or (directory / "views" / name).stat().st_size < 100:
                    raise RuntimeError("PNG export failed for " + view["id"])
                result["views"].append({"id": view["id"], "file": "views/" + name,
                    "roi": geometry.projection(center, extent, framing, view, profile["size"]),
                    "location": position, "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                    "matrices": geometry.matrices(framing, view)})
            if file_stamps(task["files"]) != source_before:
                raise ValueError("Source changed during capture")
        finally:
            with timings.measure("cleanup"):
                if render_target:
                    unreal.RenderingLibrary.release_render_target2d(render_target)
                actor = capture_actor = render_target = source = component = None
                if "world" in locals() and world:
                    native.destroy_capture_world(world)
                    world = None
                unreal.SystemLibrary.collect_garbage()
        # UE queues this GC for the next engine tick; verify only after that boundary.
        with timings.measure("cleanup_tick"):
            yield "cleaning"
        result["live_capture_worlds_after_cleanup"] = native.get_live_capture_world_count()
        if result["live_capture_worlds_after_cleanup"]:
            raise RuntimeError("Capture world survived cleanup; refusing to retain target instances")
        result.update(finished_at=utcnow(), capture_seconds=time.monotonic() - started,
                      timings=timings.seconds, rendered_frames=len(profile["views"]) * (profile["warmup_frames"] + 1))
        write_json(self.path.with_suffix(".result.json"), result)


def start():
    global _worker
    match = re.search(r'-ACCWorker=(?:"([^"]+)"|(\S+))', unreal.SystemLibrary.get_command_line())
    if not match:
        raise RuntimeError("ACCWorker requires a job file")
    path = Path(match.group(1) or match.group(2))
    config = read_json(path)
    if config.get("mode") == "initialize_scene":
        scene = "/AssetCaptureCompare/Maps/CaptureDefault"
        if not unreal.EditorAssetLibrary.does_asset_exist(scene):
            if not unreal.EditorLevelLibrary.new_level_from_template(scene, "/Engine/Maps/Templates/Template_Default"):
                raise RuntimeError("Default scene creation failed")
        write_json(path.with_suffix(".result.json"), {"status": "success", "scene": scene})
        unreal.SystemLibrary.quit_editor()
        return
    _worker = Worker(path)
    write_json(path.with_suffix(".ready.json"), {"pid": os.getpid(), "engine": unreal.SystemLibrary.get_engine_version(),
               "world": unreal.EditorLevelLibrary.get_editor_world().get_path_name()})
    unreal.log("[AssetCaptureCompare] Worker ready: {}".format(path))
