import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))


class MobilityTests(unittest.TestCase):
    def test_capture_instance_and_nested_components_become_movable(self):
        scene_component = object()
        api = SimpleNamespace(SceneComponent=scene_component, ComponentMobility=SimpleNamespace(STATIC="static", MOVABLE="movable"))
        spec = importlib.util.spec_from_file_location("acc.mobility_test_worker", Path(__file__).resolve().parents[1] / "Content/Python/acc/worker.py")
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"unreal": api}):
            spec.loader.exec_module(worker)

        class Component:
            def __init__(self, mobility):
                self.mobility = mobility

            def get_editor_property(self, name):
                if name != "mobility":
                    raise AssertionError(name)
                return self.mobility

            def set_mobility(self, value):
                self.mobility = value

        root, mesh, child_mesh, movable = [Component(value) for value in ("static", "static", "stationary", "movable")]
        actor, child, nested = Mock(), Mock(), Mock()
        actor.get_all_child_actors.return_value = [child, nested]
        actor.get_components_by_class.return_value = [root, mesh]
        child.get_components_by_class.return_value = [movable]
        nested.get_components_by_class.return_value = [child_mesh]
        result = worker.make_movable(actor)
        self.assertEqual(result["policy"], "movable")
        self.assertEqual(result["scene_components"], 4)
        self.assertEqual(result["changed_components"], 3)
        self.assertEqual(result["movable_components"], 4)
        self.assertTrue(result["verified"])
        self.assertTrue(all(c.mobility == "movable" for c in (root, mesh, child_mesh, movable)))
        actor.get_all_child_actors.assert_called_once_with(True)
        for owner in (actor, child, nested):
            owner.get_components_by_class.assert_called_once_with(scene_component)

    def test_static_mode_respects_skeletal_and_attached_component_constraints(self):
        class Component:
            def __init__(self, parent=None):
                self.mobility, self.parent = "movable", parent
            def get_editor_property(self, name):
                return self.mobility
            def set_mobility(self, value):
                self.mobility = value
            def get_attach_parent(self):
                return self.parent
        class SkeletalComponent(Component):
            pass
        api = SimpleNamespace(SceneComponent=Component, SkeletalMeshComponent=SkeletalComponent,
                              ComponentMobility=SimpleNamespace(STATIC="static", MOVABLE="movable"))
        spec = importlib.util.spec_from_file_location("acc.static_test_worker", Path(__file__).resolve().parents[1] / "Content/Python/acc/worker.py")
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"unreal": api}):
            spec.loader.exec_module(worker)
        root, mesh, skeletal = Component(), Component(), SkeletalComponent()
        attached = Component(skeletal)
        actor = Mock()
        actor.get_all_child_actors.return_value = []
        actor.get_components_by_class.return_value = [root, mesh, skeletal, attached]
        result = worker.apply_capture_mobility(actor, False)
        self.assertEqual([c.mobility for c in (root, mesh, skeletal, attached)], ["static", "static", "movable", "movable"])
        self.assertEqual(result["policy"], "static")
        self.assertEqual(result["static_components"], 2)
        self.assertEqual(result["static_unsupported_components"], 2)

    def test_default_static_skylight_is_dynamic_only_in_movable_mode(self):
        api = SimpleNamespace(SkyLightComponent=object(), ComponentMobility=SimpleNamespace(STATIC="static", MOVABLE="movable"),
                              SkyLightSourceType=SimpleNamespace(SLS_CAPTURED_SCENE="scene"))
        sky = Mock()
        properties = dict(mobility="static", sky_distance_threshold=150000.0, source_type="scene")
        sky.get_editor_property.side_effect = properties.__getitem__
        sky.set_mobility.side_effect = lambda value: properties.__setitem__("mobility", value)
        sky.set_editor_property.side_effect = properties.__setitem__
        owner = Mock()
        owner.get_components_by_class.return_value = [sky]
        owner.get_actor_label.return_value = "SkyLight"
        api.AssetCaptureCompareLibrary = Mock()
        api.AssetCaptureCompareLibrary.get_capture_actors.return_value = [owner]
        spec = importlib.util.spec_from_file_location("acc.lighting_test_worker", Path(__file__).resolve().parents[1] / "Content/Python/acc/worker.py")
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"unreal": api}):
            spec.loader.exec_module(worker)
        worker.configure_capture_lighting("world", False)
        sky.set_mobility.assert_not_called()
        sky.recapture_sky.assert_not_called()
        sky.set_editor_property.assert_not_called()
        result = worker.configure_capture_lighting("world", True)
        sky.set_mobility.assert_called_once_with("movable")
        sky.recapture_sky.assert_called_once_with()
        self.assertEqual(result["skylights"][0]["after"], "movable")
        self.assertEqual(result["skylights"][0]["sky_distance_before"], 150000.0)
        self.assertEqual(result["skylights"][0]["sky_distance_after"], 0.0)
        # An authored dynamic sky or specified cubemap keeps its capture range.
        properties.update(mobility="movable", sky_distance_threshold=4000.0)
        sky.set_editor_property.reset_mock()
        worker.configure_capture_lighting("world", True)
        sky.set_editor_property.assert_not_called()
        properties.update(mobility="static", source_type="cubemap")
        worker.configure_capture_lighting("world", True)
        sky.set_editor_property.assert_not_called()

    def test_environment_capture_excludes_targets_and_restores_visibility_on_failure(self):
        actor, child = Mock(), Mock()
        actor.get_all_child_actors.return_value = [child]
        actor.get_editor_property.return_value = False
        child.get_editor_property.return_value = True
        native = Mock()
        def prepare(world):
            actor.set_actor_hidden_in_game.assert_called_with(True)
            child.set_actor_hidden_in_game.assert_called_with(True)
            raise RuntimeError("resource failure")
        native.prepare_resources.side_effect = prepare
        spec = importlib.util.spec_from_file_location("acc.visibility_test_worker", Path(__file__).resolve().parents[1] / "Content/Python/acc/worker.py")
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"unreal": SimpleNamespace(AssetCaptureCompareLibrary=native)}):
            spec.loader.exec_module(worker)
        with self.assertRaisesRegex(RuntimeError, "resource failure"):
            worker.prepare_capture_resources("world", actor)
        actor.set_actor_hidden_in_game.assert_called_with(False)
        child.set_actor_hidden_in_game.assert_called_with(True)


if __name__ == "__main__":
    unittest.main()
