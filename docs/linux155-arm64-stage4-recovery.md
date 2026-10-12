# Linux155 ARM64 第4阶段检查点续编

## 故障与固定来源

原run `38147746377` 第5阶段编译到最终 `chrome` 链接，因 `ManagedProfileCreationController::CreateManagedProfileForTesting` 未定义失败。原因与x64相同：去signin上游补丁将真实实现从enterprise GN target移除，但原调用保留。

失败阶段只保存诊断，最新有效文件树是第4阶段：

- donor提交 `e1263e2a52ea8eb7fa4f6c3a9a1930b92a8afe7e`；
- run `38147746377`，attempt1，stage4，producer job `114654120821`；
- artifact `11734746823`，字节数 `5307317175`；
- SHA-256 `5f3bc2d96d45cc250f12b5da1a193b40dcf7c15f24e806c8628fb6b14d8c978c`。

真实GN为Linux/ARM64、PGO0，与本次fast profile一致。该缓存不能用于x64或其他版本。

## 精确迁移

恢复分支 `build/linux155-arm64-stage4-recovery` 从原donor创建，仅携带已验证的两项Linux155补丁及恢复校验：

1. `0225-managed-profile-controller-link.patch`：将原真实controller实现重新加入构建，不添加stub或删除调用。
2. `0227-opaque-png-export-consistency.patch`：仅规范化不透明PNG异步导出，保留原字节一致性门禁。

两项已在x64的成功运行 `38224093790` 通过原生device/canvas门禁，但ARM64仍需自己编译及原生验证，不能直接移用x64结论。

旧224补丁前缀与固定core/platform、准备工具、版本、GN args保持不变。新的ARM64迁移策略只认证上述精确donor、目标配置与两份补丁；不扩大原x64恢复例外。临时源码的反向/正向校验通过后，才发布源文件和新收据。

## 执行边界

恢复仍通过已验证文件树进行，不允许冷准备或退回上游重新开始。原对象、Ninja日志和此前四阶段进度保留；第5阶段未上传的工作只能重做。

工作流显示的新“第1阶段”是恢复运行的编号，不表示重新下载Chromium全量构建。完成交叉编译后还需要原生ARM64运行验证及最终归档来源核验，在此之前不发布。
