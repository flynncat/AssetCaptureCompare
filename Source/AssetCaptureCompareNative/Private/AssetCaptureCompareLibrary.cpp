#include "AssetCaptureCompareLibrary.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "Engine/Level.h"
#include "Engine/MapBuildDataRegistry.h"
#include "Misc/PackageName.h"
#include "ContentStreaming.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "Modules/ModuleManager.h"
#include "RHI.h"
#include "DynamicRHI.h"
#include "ShaderCompiler.h"
#include "RenderingThread.h"
#include "Engine/Blueprint.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/Texture2D.h"
#include "Animation/SkeletalMeshActor.h"
#include "Engine/SceneCapture2D.h"
#include "Components/StaticMeshComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Components/SkyLightComponent.h"
#include "EngineUtils.h"
#include "UObject/Package.h"
#include "Misc/Guid.h"

IMPLEMENT_MODULE(FDefaultModuleImpl, AssetCaptureCompareNative)

namespace
{
    TArray<TWeakObjectPtr<UWorld>> CaptureWorlds;
    bool IsCaptureWorker()
    {
        FString Value;
        return IsInGameThread() && FParse::Value(FCommandLine::Get(), TEXT("ACCWorker="), Value);
    }
}

UWorld* UAssetCaptureCompareLibrary::CreateCaptureWorld(const FString& SceneObjectPath)
{
    if (!IsCaptureWorker())
    {
        return nullptr;
    }
    UWorld* Template = LoadObject<UWorld>(nullptr, *SceneObjectPath);
    if (!Template || !Template->PersistentLevel || Template->GetStreamingLevels().Num() > 0)
    {
        return nullptr;
    }
    UMapBuildDataRegistry* FrozenBuildData = nullptr;
    const FString DataPackage = FPackageName::ObjectPathToPackageName(SceneObjectPath) + TEXT("_BuiltData");
    if (FPackageName::DoesPackageExist(DataPackage))
    {
        const FString DataPath = DataPackage + TEXT(".") + FPackageName::GetShortName(DataPackage);
        FrozenBuildData = LoadObject<UMapBuildDataRegistry>(nullptr, *DataPath);
        if (!FrozenBuildData)
        {
            return nullptr;
        }
    }
    else if (Template->PersistentLevel->MapBuildData)
    {
        UE_LOG(LogTemp, Error, TEXT("[AssetCaptureCompare] Missing frozen external BuiltData: %s"), *DataPackage);
        return nullptr;
    }
    UPackage* Package = CreatePackage(*FString::Printf(TEXT("/Temp/ACC_%s"), *FGuid::NewGuid().ToString(EGuidFormats::Digits)));
    Package->SetFlags(RF_Transient);
    UWorld* World = Cast<UWorld>(StaticDuplicateObject(Template, Package, Template->GetFName()));
    if (!World)
    {
        return nullptr;
    }
    // NonPIEDuplicateTransient clears this reference during preview-world duplication.
    World->PersistentLevel->MapBuildData = FrozenBuildData;
    UE_LOG(LogTemp, Log, TEXT("[AssetCaptureCompare] Frozen BuiltData: %s"),
        FrozenBuildData ? *FrozenBuildData->GetPathName() : TEXT("none"));
    World->WorldType = EWorldType::EditorPreview;
    World->ClearFlags(RF_Standalone | RF_Public);
    World->SetFlags(RF_Transient);
    World->AddToRoot();
    GEngine->CreateNewWorldContext(EWorldType::EditorPreview).SetCurrentWorld(World);
    World->InitWorld(UWorld::InitializationValues().AllowAudioPlayback(false).CreatePhysicsScene(true)
        .CreateNavigation(false).CreateAISystem(false).ShouldSimulatePhysics(false).SetTransactional(false));
    World->UpdateWorldComponents(true, false);
    CaptureWorlds.Add(World);
    return World;
}

void UAssetCaptureCompareLibrary::DestroyCaptureWorld(UWorld* World)
{
    if (IsCaptureWorker() && World && World->WorldType == EWorldType::EditorPreview)
    {
        World->DestroyWorld(false);
        GEngine->DestroyWorldContext(World);
        World->ReleasePhysicsScene();
        World->MarkObjectsPendingKill();
        FlushRenderingCommands();
    }
}

AActor* UAssetCaptureCompareLibrary::SpawnCaptureAsset(UWorld* World, UObject* Asset)
{
    if (!IsCaptureWorker() || !World || !Asset)
    {
        return nullptr;
    }
    FActorSpawnParameters Parameters;
    Parameters.ObjectFlags = RF_Transient;
    Parameters.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
    const FTransform Transform = FTransform::Identity;
    AActor* Actor = nullptr;
    if (UStaticMesh* Mesh = Cast<UStaticMesh>(Asset))
    {
        AStaticMeshActor* MeshActor = World->SpawnActor<AStaticMeshActor>(AStaticMeshActor::StaticClass(), Transform, Parameters);
        MeshActor->GetStaticMeshComponent()->SetMobility(EComponentMobility::Movable);
        MeshActor->GetStaticMeshComponent()->SetStaticMesh(Mesh);
        Actor = MeshActor;
    }
    else if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(Asset))
    {
        ASkeletalMeshActor* MeshActor = World->SpawnActor<ASkeletalMeshActor>(ASkeletalMeshActor::StaticClass(), Transform, Parameters);
        MeshActor->GetSkeletalMeshComponent()->SetSkeletalMesh(SkeletalMesh);
        Actor = MeshActor;
    }
    else if (UBlueprint* Blueprint = Cast<UBlueprint>(Asset))
    {
        UClass* Class = Blueprint->GeneratedClass;
        if (Blueprint->Status != BS_Error && Class && Class->IsChildOf(AActor::StaticClass()) && !Class->HasAnyClassFlags(CLASS_Abstract | CLASS_Deprecated))
        {
            Actor = World->SpawnActor(Class, &Transform, Parameters);
        }
    }
    return Actor;
}

AActor* UAssetCaptureCompareLibrary::SpawnCaptureCamera(UWorld* World)
{
    if (!IsCaptureWorker() || !World)
    {
        return nullptr;
    }
    FActorSpawnParameters Parameters;
    Parameters.ObjectFlags = RF_Transient;
    return World->SpawnActor<ASceneCapture2D>(ASceneCapture2D::StaticClass(), FTransform::Identity, Parameters);
}

TArray<AActor*> UAssetCaptureCompareLibrary::GetCaptureActors(UWorld* World)
{
    TArray<AActor*> Actors;
    if (World && IsCaptureWorker())
    {
        for (TActorIterator<AActor> Iterator(World); Iterator; ++Iterator)
        {
            Actors.Add(*Iterator);
        }
    }
    return Actors;
}

bool UAssetCaptureCompareLibrary::PrepareResources(UObject* WorldContextObject)
{
    if (!IsCaptureWorker() || !WorldContextObject)
    {
        return false;
    }
    if (GShaderCompilingManager)
    {
        GShaderCompilingManager->FinishAllCompilation();
    }
    UWorld* World = GEngine->GetWorldFromContextObject(WorldContextObject, EGetWorldErrorMode::ReturnNull);
    if (!World)
    {
        return false;
    }
    TSet<UTexture2D*> TexturesToVerify;
    for (TActorIterator<AActor> Iterator(World); Iterator; ++Iterator)
    {
        TInlineComponentArray<UMeshComponent*> Meshes;
        Iterator->GetComponents(Meshes);
        for (UMeshComponent* Mesh : Meshes)
        {
            Mesh->SetTextureForceResidentFlag(true);
            TArray<UTexture*> Textures;
            Mesh->GetUsedTextures(Textures, EMaterialQualityLevel::Num);
            for (UTexture* Texture : Textures)
            {
                if (UTexture2D* Texture2D = Cast<UTexture2D>(Texture))
                {
                    TexturesToVerify.Add(Texture2D);
                }
            }
        }
    }
    World->SendAllEndOfFrameUpdates();
    USkyLightComponent::UpdateSkyCaptureContents(World);
    const int32 Pending = IStreamingManager::Get().StreamAllResources(30.0f);
    for (UTexture2D* Texture : TexturesToVerify)
    {
        Texture->WaitForStreaming(true);
        if (!Texture->IsFullyStreamedIn())
        {
            UE_LOG(LogTemp, Warning, TEXT("[AssetCaptureCompare] Texture not fully resident: %s"), *Texture->GetPathName());
            return false;
        }
    }
    FlushRenderingCommands();
    return Pending == 0 && (!GShaderCompilingManager || !GShaderCompilingManager->IsCompiling());
}

void UAssetCaptureCompareLibrary::FreezeCaptureTime(UObject* WorldContextObject, float Seconds)
{
    if (!IsCaptureWorker())
    {
        return;
    }
    UWorld* World = GEngine->GetWorldFromContextObject(WorldContextObject, EGetWorldErrorMode::ReturnNull);
    if (World)
    {
        World->TimeSeconds = Seconds;
        World->UnpausedTimeSeconds = Seconds;
        World->RealTimeSeconds = Seconds;
        World->DeltaTimeSeconds = 0.0f;
        FMath::RandInit(0);
        FMath::SRandInit(0);
    }
}

FString UAssetCaptureCompareLibrary::GetRenderEnvironment()
{
    return FString::Printf(TEXT("%s|%s"), GDynamicRHI ? GDynamicRHI->GetName() : TEXT("None"), *GRHIAdapterName);
}

int32 UAssetCaptureCompareLibrary::GetLiveCaptureWorldCount()
{
    CaptureWorlds.RemoveAll([](const TWeakObjectPtr<UWorld>& World) { return !World.IsValid(true); });
    return CaptureWorlds.Num();
}
