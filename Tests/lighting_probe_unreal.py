"""UE-only lighting regression: original, Movable and Static cubes; never saves assets."""
import json
from pathlib import Path
import unreal
from acc import geometry
from acc.common import DEFAULT_PROFILE, write_json
from acc.worker import apply_capture_mobility, configure_capture_lighting, prepare_capture_resources

assert "-ACCWorker=" in unreal.SystemLibrary.get_command_line()
root = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())).resolve()
output = root / "Saved/AssetCaptureCompareLightingProbe"
output.mkdir(exist_ok=True)
native = unreal.AssetCaptureCompareLibrary
report = []


def probe():
    for mode in ("original", "movable", "static"):
        world = native.create_capture_world("/AssetCaptureCompare/Maps/CaptureDefault.CaptureDefault")
        assert world
        lights = []
        for owner in native.get_capture_actors(world):
            for light in owner.get_components_by_class(unreal.LightComponentBase):
                lights.append(dict(actor=owner.get_actor_label(), type=light.get_class().get_name(),
                                   mobility=str(light.get_editor_property("mobility")),
                                   intensity=light.get_editor_property("intensity")))
                if isinstance(light, unreal.SkyLightComponent):
                    lights[-1].update(source=str(light.get_editor_property("source_type")),
                                      cubemap=str(light.get_editor_property("cubemap")),
                                      shadows=light.get_editor_property("cast_shadows"),
                                      distance=light.get_editor_property("sky_distance_threshold"),
                                      location=str(light.get_world_location()))
        lighting = configure_capture_lighting(world, mode == "movable") if mode != "original" else None
        actor = native.spawn_capture_asset(world, unreal.load_asset("/Engine/BasicShapes/Cube.Cube"))
        mobility = apply_capture_mobility(actor, mode != "static")
        center, extent, radius = unreal.SystemLibrary.get_component_bounds(actor.get_component_by_class(unreal.StaticMeshComponent))
        framing = geometry.frame([center.x, center.y, center.z], [extent.x, extent.y, extent.z], DEFAULT_PROFILE)
        camera = native.spawn_capture_camera(world)
        component = camera.get_component_by_class(unreal.SceneCaptureComponent2D)
        target = unreal.RenderingLibrary.create_render_target2d(world, 480, 480, unreal.TextureRenderTargetFormat.RTF_RGBA8)
        component.set_editor_property("texture_target", target)
        component.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        component.set_editor_property("capture_every_frame", False)
        component.set_editor_property("capture_on_movement", False)
        component.set_editor_property("fov_angle", DEFAULT_PROFILE["fov"])
        component.set_editor_property("hidden_actors", [a for a in native.get_capture_actors(world) if a.get_actor_label() == "Floor"])
        post = component.get_editor_property("post_process_settings")
        for key, value in (("override_auto_exposure_min_brightness", True), ("override_auto_exposure_max_brightness", True),
                           ("auto_exposure_min_brightness", 1.0), ("auto_exposure_max_brightness", 1.0)):
            post.set_editor_property(key, value)
        component.set_editor_property("post_process_settings", post)
        assert prepare_capture_resources(world, actor)
        for view in (DEFAULT_PROFILE["views"][1], DEFAULT_PROFILE["views"][5]):
            outward, _, _ = geometry.basis(view)
            position = unreal.Vector(*[framing["center"][i] + framing["distance"] * outward[i] for i in range(3)])
            rotation = unreal.MathLibrary.find_look_at_rotation(position, unreal.Vector(*framing["center"]))
            camera.set_actor_location_and_rotation(position, rotation, False, True)
            native.freeze_capture_time(world, 1.0)
            component.capture_scene()
            yield
            name = mode + "_" + view["id"] + ".png"
            unreal.RenderingLibrary.export_render_target(world, target, str(output), name)
        report.append(dict(mode=mode, lights=lights, target=mobility, capture_lighting=lighting))
        unreal.RenderingLibrary.release_render_target2d(target)
        native.destroy_capture_world(world)
        world = actor = camera = component = target = light = owner = None
        unreal.SystemLibrary.collect_garbage()
        yield
        yield
    write_json(output / "report.json", report)


steps = probe()


def tick(delta):
    try:
        next(steps)
    except StopIteration:
        unreal.unregister_slate_post_tick_callback(handle)
        unreal.SystemLibrary.quit_editor()
    except Exception:
        import traceback
        write_json(output / "error.json", {"error": traceback.format_exc()})
        unreal.unregister_slate_post_tick_callback(handle)
        unreal.SystemLibrary.quit_editor()


handle = unreal.register_slate_post_tick_callback(tick)
