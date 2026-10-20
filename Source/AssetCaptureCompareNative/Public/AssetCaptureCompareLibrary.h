#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "AssetCaptureCompareLibrary.generated.h"

UCLASS()
class ASSETCAPTURECOMPARENATIVE_API UAssetCaptureCompareLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category="Asset Capture Compare")
    static UWorld* CreateCaptureWorld(const FString& SceneObjectPath);

    UFUNCTION(BlueprintCallable, Category="Asset Capture Compare")
    static void DestroyCaptureWorld(UWorld* World);

    UFUNCTION(BlueprintCallable, Category="Asset Capture Compare")
    static AActor* SpawnCaptureAsset(UWorld* World, UObject* Asset);

    UFUNCTION(BlueprintCallable, Category="Asset Capture Compare")
    static AActor* SpawnCaptureCamera(UWorld* World);

    UFUNCTION(BlueprintPure, Category="Asset Capture Compare")
    static TArray<AActor*> GetCaptureActors(UWorld* World);

    UFUNCTION(BlueprintCallable, Category="Asset Capture Compare")
    static bool PrepareResources(UObject* WorldContextObject);

    UFUNCTION(BlueprintCallable, Category="Asset Capture Compare")
    static void FreezeCaptureTime(UObject* WorldContextObject, float Seconds = 1.0f);

    UFUNCTION(BlueprintPure, Category="Asset Capture Compare")
    static FString GetRenderEnvironment();

    UFUNCTION(BlueprintPure, Category="Asset Capture Compare")
    static int32 GetLiveCaptureWorldCount();
};
