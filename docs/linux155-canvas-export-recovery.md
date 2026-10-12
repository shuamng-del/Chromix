# Linux155 不透明 PNG 导出一致性与增量恢复

## 真实失败与独立解码

运行 `38204495350` 已完成最终Chrome链接和打包，候选版本155.0.8059.39。门禁在 `device` / `canvas` 的不透明 HTML PNG 导出一致性检查失败。

- 二进制SHA256：`77f87267e5b3fe5d3b253a39ae5ab739d1fc32ed10f5d76f5dfaac6c61c983b8`。
- ZIP SHA256：`a85b268f51ff684945a9adab9522c44aca950d3e9b7fff3b8e891135554067f7`。
- 接受报告源码commit：`12a501c7abe607552c49d5daaabf29d891dde9ae`，Linux155平台225补丁。

对报告中20对失败payload分别做PNG CRC/解滤波以及Pillow/LittleCMS独立解码后，原始RGBA、ICC和转换后的sRGB均一致；与opaque reference逐通道最大差为0。每个API单独重复调用也稳定。差异是 `toBlob` 导出RGB PNG（color type2），`toDataURL` 导出RGBA PNG（color type6），不是随机元数据、时间戳或像素损坏。

错误文本“DataURL repeat/Blob mismatch”合并了多个条件。本案的 `urlRepeat` 为true、`urlBlobMatches` 为false，不应被叙述成重复调用不稳定。

## 固定源码机制

Chromium155的 `GetColorInfoForEncoder` 将opaque/premul设为unpremul。同步ImageDataBuffer比较完整colorInfo并实际转换；异步CanvasAsyncBlobCreator为保留encoder-side unpremultiply优化，在转换判断中暂时恢复源alpha type，因此忽略仅alpha标签的差别。

结果是同色域RGBA8不透明输入：同步编码器收到unpremul pixmap，异步仍为opaque。固定Skia `29ed1e87a0a50f3d8347e988842d9d59e7573efa` 据此分别编码RGBA和RGB。两路compression level相同，P3 ICC也相同。

报告是在 `fingerprint=off` / native / disable-noise 下生成的，不能将本案归因于噪声二次应用，也不能开启noise恰好使格式相同就当作修复。

## 本次项目契约选择

本项目已有跨API PNG字节一致性门禁，本次保留该门禁，而非将它改成只比较像素。新增Linux155专用 `0227-opaque-png-export-consistency.patch` 仅在异步**PNG且opaque输入**时执行与同步路径一致的真实颜色/alpha转换。

保留premul输入的原生encoder-side优化；不改变JPEG/WebP处理、ICC、源画布内容、透明像素或噪声开关。这是明确的编码规范化选择，不能描述为已观测的像素损坏修复或stock浏览器实测结论。

基础224补丁与其他平台选择不变；Linux155从旧225补丁精确迁移到226。旧schema1选择身份仍保持，以认证原已编译检查点；新schema只允许已有controller实现修复及此精确PNG修复，任意缺文件/额外补丁/哈希不符仍拒绝。

## 恢复来源与验收

- run `38204495350`，stage1，attempt1；
- donor `12a501c7abe607552c49d5daaabf29d891dde9ae`；
- 文件树artifact `11733517946`，SHA256 `8b1639700a45bb60759857c9fee6ff0d905d09c4b2e93b0e623278a03643b359`；
- 恢复分支 `build/linux155-compiled-png-recovery`。

该检查点已包含完整编译产物。修复后仅重编受影响的Canvas导出单元并增量链接；不丢弃对象缓存，不回退到上游全量准备。

静态源码重建、条件分支C++测试和旧payload独立解码不能代替新的Skia真实转换及原生验收。新候选仍需重跑原device/canvas门禁、同步/异步/worker、sRGB/P3、透明度及新像素噪声专项；在通过前保持unaccepted，不覆写Release。
