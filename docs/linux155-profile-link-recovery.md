# Linux155 企业 profile 实现链接与检查点恢复

## 故障证据

Linux x64 run `38149695142` 在第3阶段最终 `chrome` 链接失败。新编译的 `profile_management_disclaimer_service.o` 引用了 `ManagedProfileCreationController::CreateManagedProfileForTesting(...)`，但链接图中没有该实现。

固定 Chromium155原源码包含匹配的声明和真实实现。Core `ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a` 的 `remove-unused-preferences-fields.patch` 从 enterprise `impl` 删除了 `signin/managed_profile_creation_controller.cc`，同时保留了对 testing factory 的原有调用。`CHECK_IS_TEST()` 是运行时检查，不会消除链接依赖。

Windows 平台层已经单独处理了这一不对称，因此本次只修复精确 Linux155 平台，不改变 Windows154/155、macOS152/154 或根224补丁序列。

## 最小修复

`patches/chromium155-linux/0225-managed-profile-controller-link.patch` 将现有真实实现重新加入原 enterprise source set。只恢复一行GN source，不删除调用、不增加stub、不改链接器忽略未定义符号选项，也不回退core的去signin修改。

`build/patch-additions-linux155.json` 和源码选择器绑定该新增补丁的完整摘要、固定平台提交与版本。Linux155有效序列变为225；其他平台仍为224。历史 donor 没有新增清单和目录时保留旧224身份，以供经过Git提交和收据认证的迁移使用。

真实 Linux 依赖编译、GN include检查和最终链接尚需云端执行。孤立factory测试只能验证提取定义及匹配签名的符号链接，不能代表完整Chromium编译。

## 恢复来源

失败阶段只上传诊断，最新可用文件树为：

- donor run `38149695142`，stage 2，attempt 1；
- donor commit `e1263e2a52ea8eb7fa4f6c3a9a1930b92a8afe7e`；
- artifact `11718904327`，`chromix-linux-x64-tree-s2-attempt-1-part1`；
- SHA-256 `b7fed66f3f114499b8f138d62587f7074791d995f8b6b31fea83b76ff2d4d11a`。

目标恢复分支基于同一donor源码，只加入GN修复和明确的selector迁移授权，不混入后续Windows宿主依赖及macOS诊断改动。

迁移保留所有其他固定版本与准备工具字节约束；新selector读取历史donor时必须生成原身份及准备key，目标必须精确等于旧224补丁加一份固定新增补丁。私有scratch的反向/正向校验通过后，才发布GN文件和更新收据；对象缓存和Ninja状态保持原位，源文件时间戳推进以触发真实增量重编。

不得直接重新运行旧失败job当作修复，也不得恢复后删掉全部 `out/Default` 或退回上游冷构建。重编原失败阶段丢失的工作是无法避免的，但第1–2阶段保留的输出继续复用。
