using UnrealBuildTool;

public class AssetCaptureCompareNative : ModuleRules
{
    public AssetCaptureCompareNative(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Engine" });
        PrivateDependencyModuleNames.AddRange(new[] { "UnrealEd", "RenderCore", "RHI" });
    }
}
