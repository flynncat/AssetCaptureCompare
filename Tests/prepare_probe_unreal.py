"""Runs only inside the disposable clean-project acceptance editor."""
import json
from pathlib import Path
import unreal
from acc import editor

root = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())).resolve()
assert root.name == "ACCCleanAcceptance", "Probe generation is restricted to the disposable project"
tools = unreal.AssetToolsHelpers.get_asset_tools()
mesh = unreal.load_asset("/Game/ACCProbe/ProbeCube") if unreal.EditorAssetLibrary.does_asset_exist("/Game/ACCProbe/ProbeCube") else unreal.EditorAssetLibrary.duplicate_asset("/Engine/BasicShapes/Cube", "/Game/ACCProbe/ProbeCube")
build = unreal.EditorStaticMeshLibrary.get_lod_build_settings(mesh, 0)
build.set_editor_property("build_scale3d", unreal.Vector(1, 1, 1))
unreal.EditorStaticMeshLibrary.set_lod_build_settings(mesh, 0, build)
material = unreal.load_asset("/Game/ACCProbe/ProbeMaterial") if unreal.EditorAssetLibrary.does_asset_exist("/Game/ACCProbe/ProbeMaterial") else None
if not material:
    material = tools.create_asset("ProbeMaterial", "/Game/ACCProbe", unreal.Material, unreal.MaterialFactoryNew())
    color = unreal.MaterialEditingLibrary.create_material_expression(material, unreal.MaterialExpressionVectorParameter, -200, 0)
    color.set_editor_property("parameter_name", "ProbeColor")
    color.set_editor_property("default_value", unreal.LinearColor(.1, .5, .8, 1))
    unreal.MaterialEditingLibrary.connect_material_property(color, "", unreal.MaterialProperty.MP_BASE_COLOR)
    unreal.MaterialEditingLibrary.recompile_material(material)
instance = unreal.load_asset("/Game/ACCProbe/ProbeInstance") if unreal.EditorAssetLibrary.does_asset_exist("/Game/ACCProbe/ProbeInstance") else tools.create_asset("ProbeInstance", "/Game/ACCProbe", unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
unreal.MaterialEditingLibrary.set_material_instance_parent(instance, material)
unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(instance, "ProbeColor", unreal.LinearColor(.1, .5, .8, 1))
mesh.set_material(0, instance)
unreal.EditorAssetLibrary.save_directory("/Game/ACCProbe", only_if_is_dirty=False, recursive=True)
prepared = editor.handle({"action": "prepare", "paths": ["/Game/ACCProbe/ProbeCube.ProbeCube"], "scene": "/AssetCaptureCompare/Maps/CaptureDefault"})
(root / "Saved").mkdir(exist_ok=True)
(root / "Saved/prepared.json").write_text(json.dumps(prepared), encoding="utf-8")
