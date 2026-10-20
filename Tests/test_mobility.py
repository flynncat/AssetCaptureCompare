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
        api = SimpleNamespace(SceneComponent=scene_component, ComponentMobility=SimpleNamespace(MOVABLE="movable"))
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
        self.assertEqual(result, {"policy": "movable", "scene_components": 4, "changed_components": 3})
        self.assertTrue(all(c.mobility == "movable" for c in (root, mesh, child_mesh, movable)))
        actor.get_all_child_actors.assert_called_once_with(True)
        for owner in (actor, child, nested):
            owner.get_components_by_class.assert_called_once_with(scene_component)


if __name__ == "__main__":
    unittest.main()
