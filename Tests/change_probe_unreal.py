"""Apply a visible material change without any dependency on the capture tool."""
from pathlib import Path
import unreal

root = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())).resolve()
assert root.name == "ACCCleanAcceptance"
instance = unreal.load_asset("/Game/ACCProbe/ProbeInstance.ProbeInstance")
unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(instance, "ProbeColor", unreal.LinearColor(.8, .05, .08, 1))
unreal.EditorAssetLibrary.save_loaded_asset(instance, only_if_is_dirty=False)
(root / "Saved/changed.txt").write_text("material changed", encoding="ascii")
