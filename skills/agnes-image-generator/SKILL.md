# Agnes Image Generator

## 描述
使用 Agnes Image 2.1 Flash API 生成高质量图像，支持文生图和图生图。

## 触发词
生成图片、画图、image、generate image、画图给我

## API 信息

- **Endpoint**: `https://apihub.agnes-ai.com/v1/images/generations`
- **Method**: POST
- **Model**: `agnes-image-2.1-flash`
- **API Key**: `sk-kdlFTHAbe1mGxJeqRoyWnW4by1qdCIbD337M1L6Uul1Hfj5b`

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| model | string | 是 | 固定为 `agnes-image-2.1-flash` |
| prompt | string | 是 | 图像生成提示词 |
| size | string | 否 | 输出尺寸，如 `1024x768`、`1024x1536` |
| extra_body | object | 否 | 额外参数，包含 `response_format: "url"` |

## 使用方式

当用户要求生成图片时：

1. 理解用户想要的画面内容
2. 用英文编写详细的 prompt（参考推荐结构：主体 + 场景 + 风格 + 光照 + 构图 + 质量要求）
3. 确定图片尺寸（默认 1024x768，竖屏用 1024x1536）
4. 调用 Python 执行 API 请求
5. 返回生成的图片 URL

## 推荐 Prompt 结构

```
[主体] + [场景/环境] + [风格] + [光照] + [构图] + [质量要求]
```

## 示例

### 文生图
```python
import requests
import json

url = "https://apihub.agnes-ai.com/v1/images/generations"
headers = {
    "Authorization": "Bearer sk-kdlFTHAbe1mGxJeqRoyWnW4by1qdCIbD337M1L6Uul1Hfj5b",
    "Content-Type": "application/json"
}
payload = {
    "model": "agnes-image-2.1-flash",
    "prompt": "A luminous floating city above a misty canyon at sunrise, cinematic realism",
    "size": "1024x768",
    "extra_body": {
        "response_format": "url"
    }
}

response = requests.post(url, headers=headers, json=payload)
result = response.json()
image_url = result["data"][0]["url"]
print(image_url)
```

### 图生图
```python
payload = {
    "model": "agnes-image-2.1-flash",
    "prompt": "Transform the scene into a rain-soaked cyberpunk night",
    "size": "1024x768",
    "extra_body": {
        "image": ["https://example.com/input-image.png"],
        "response_format": "url"
    }
}
```

## 注意事项

- API Key 已内置，无需用户提供
- 图片以 URL 形式返回，可直接展示
- 默认价格为 $0.003/张（当前免费）
- 复杂图像建议使用更详细的 prompt
