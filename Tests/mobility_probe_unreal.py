"""Verify real Static -> Movable conversion without saving any Unreal assets."""
from pathlib import Path

import unreal

from acc.common import write_json
from acc.worker import make_movable


root = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())).resolve()
assert root.name == "ACCCleanAcceptance", "Run only in the disposable acceptance project"
assert "-ACCWorker=" in unreal.SystemLibrary.get_command_line()
native = unreal.AssetCaptureCompareLibrary
world = None
try:
    world = native.create_capture_world("/AssetCaptureCompare/Maps/CaptureDefault.CaptureDefault")
    assert world
    mesh = unreal.load_asset("/Engine/BasicShapes/Cube.Cube")
    default = unreal.get_default_object(unreal.StaticMeshActor)
    template = default.get_component_by_class(unreal.StaticMeshComponent)
    template_before = template.get_editor_property("mobility")
    actor = native.spawn_capture_asset(world, mesh)
    component = actor.get_component_by_class(unreal.StaticMeshComponent)
    component.set_mobility(unreal.ComponentMobility.STATIC)
    assert component.get_editor_property("mobility") == unreal.ComponentMobility.STATIC
    result = make_movable(actor)
    assert result["changed_components"] == 1, result
    assert component.get_editor_property("mobility") == unreal.ComponentMobility.MOVABLE
    assert template.get_editor_property("mobility") == template_before
    result.update(status="success", default_object_unchanged=True)
finally:
    if world:
        native.destroy_capture_world(world)
    world = actor = component = mesh = default = template = None
    unreal.SystemLibrary.collect_garbage()


# Match the production worker's deferred GC boundary before editor shutdown.
waiting = True


def finish(delta):
    global waiting
    if waiting:
        waiting = False
        return
    unreal.unregister_slate_post_tick_callback(handle)
    result["live_capture_worlds_after_cleanup"] = native.get_live_capture_world_count()
    assert result["live_capture_worlds_after_cleanup"] == 0
    write_json(root / "Saved/mobility_probe.json", result)
    unreal.SystemLibrary.quit_editor()


handle = unreal.register_slate_post_tick_callback(finish)
