# macOS154 双架构运行时修复续编

本次使用各自已编译完成、但原生运行验收失败的文件树。保持 Chromium `154.0.8037.97`、macOS core/overlay、原 GN args 与对象缓存，不冷准备或删除整个输出目录。

## 修复内容与来源

回移 Chromium 官方提交 [`7870c327159042185b6918a1c381775fbae37b56`](https://chromium.googlesource.com/chromium/src/+/7870c327159042185b6918a1c381775fbae37b56)（Headless: Ensure browser terminates after headless command execution，CL 8339987）。

原 headless 命令回调依靠关闭窗口、等待所有 keepalive 消失；后台服务可能使进程停留在 AppKit 消息循环。新增 macOS154专属补丁调用既有 `CloseAllBrowsersAndQuit()` 正常退出流程，保留 profile keepalive 生命周期及 Mac 应用允许退出调用；没有调用 `_exit`、伪造返回码或跳过关闭逻辑。

`patches/chromium154-macos/0226-headless-command-explicit-quit.patch` 通过 `build/patch-additions-macos154.json` 精确绑定。macOS154有效序列从224变225，根序列及历史选择身份保持。补丁只改 `chrome/browser/ui/startup/startup_browser_creator_impl.cc`，需要重编该编译单元、重新链接Framework及装配App。

Intel还需恢复普通Helper的缺失构建输出，使用已验证的精确输出修复工具；不会用其他Helper二进制冒充。ARM64既有Helper必须通过完整性检查，不能直接套用Intel专用修复。

## 独立阻塞尚未宣称解决

ARM64本机复现还发现Keychain查询在securityd等待、阻止ThreadPool完成关闭。真实Security.framework对照中，查询级非交互选项未解除该路径，因此未整合推测性Keychain补丁；不会使用mock keychain、固定密钥、明文回退或禁用provider作为生产修复。

本次官方normal-quit修复可以处理尚未发起退出的路径，但不能据此宣称Keychain teardown也解决。两种架构均必须保持sandbox、60秒自然退出要求并生成新的原生报告，失败则保留候选包和检查点，不发布。

## 固定恢复源

共同 donor 提交：`4632c980d1154ff59243b97fa1b0b816d2440eaa`。

| 架构 | 运行 / 阶段 / attempt | 文件树 artifact IDs |
|---|---|---|
| ARM64 | `38070853641` / 4 / 1 | `11695671646`, `11695277300` |
| Intel x64 | `38165640844` / 2 / 1 | `11719847390`, `11719568394` |

元数据工具固定所有大小、SHA256、生产job、workflow和源码身份。下载后继续核验实际归档，不接受过期或不完整的分卷。

## 执行流程

`.github/workflows/repair-macos154-runtime.yml` 在原生双架构runner上：

1. 严格恢复原文件树、源码收据、准备key与增量对象状态。
2. 只允许认证的224→225 selector及官方补丁变化；私有scratch反向/正向验证后才发布新源文件及收据。
3. 准备本机工具，用原args重新生成GN图，保留对象缓存；对图与编译状态变化分别记录，不把正常Ninja更新当成输入篡改。
4. 有界增量Ninja、完整App/ZIP结构验证和实际包提取。
5. 原生版本/正常退出smoke、全套fingerprint acceptance、Canvas/WebGL/GPU专项预检。
6. 候选包与证据分开上传。未通过验收的包标记unaccepted，不发布Release。失败后保留的检查点也不能跳过transaction blocker直接接着编译。

本流程不修改现有下载入口或覆写旧Release。补丁单测、完整225源码回放以及迁移测试通过，都不能代替新的原生运行验收。
