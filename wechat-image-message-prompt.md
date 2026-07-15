# 微信 iLink Bot 接入图文消息功能

## 目标
在现有 `agent_core/wechat_bot.py` 基础上，支持图文消息的**发送**和**接收**。

## 核心约束
- **发送侧**：文本和图片必须在服务端拼成同一条 `sendmessage` 的 `item_list`，一次 HTTP 请求发出。不能分两次发送。
- **接收侧**：`_handle_message()` 目前只处理 `type: 1` 文本，`type: 2` 图片消息被忽略。必须补充接收能力，下载解密后暂存到本地。
- **不改动** `send_message()` 的文本消息逻辑。

## 发送侧要求
新增 `send_rich_message(self, to_user_id, context_token, text, image_path, thumb_path=None) -> dict` 作为图文混合消息的唯一入口。

内部流程：
1. 图片走 CDN 上传 + AES-128-ECB 加密，拿到 `encrypt_query_param` 和 `aes_key`
2. 构造同时包含 `type: 1`（文本）和 `type: 2`（图片）的混合 `item_list`
3. 一次 `sendmessage` 发出

辅助方法（内部使用，不对外暴露独立发送图片的接口）：
- `_calc_media_params(file_path)`：计算 rawsize、rawfilemd5、filesize
- `_aes_ecb_encrypt(data, key)`：AES-128-ECB + PKCS7 填充
- `_upload_to_cdn(upload_param, encrypted_data)`：PUT 上传到 CDN，返回 encrypt_query_param

## 接收侧要求
修改 `_handle_message()`：
1. 遍历 `item_list` 时识别 `type: 2`（图片）
2. 提取 `image_item.cdn_media` 中的 `encrypt_query_param` 和 `aes_key`
3. 调用 CDN 下载接口获取加密图片，本地 AES-128-ECB 解密
4. 保存到临时目录（如 `self.data_dir / "incoming"`），文件名带时间戳避免冲突
5. 将图片路径作为附件记录到会话消息中，至少做到"收到并暂存，不丢消息"

## 其他要求
- 代码风格与现有 `wechat_bot.py` 一致（异步、日志、错误处理）
- 所有 API 调用携带正确请求头（`Content-Type`、`AuthorizationType`、`Authorization`、`X-WECHAT-UIN`）
- `X-WECHAT-UIN` 每次请求重新生成
- 请求体包含 `base_info: {"channel_version": "2.0.0"}`
- 错误处理：网络异常、AES 加密失败、CDN 上传失败都要有日志和合理 fallback
- 添加至少一个使用示例或测试用例，演示如何调用 `send_rich_message`
