# PPTX Generator

## 描述
使用 Node.js 和 PptxGenJS 库生成高质量 PowerPoint 演示文稿，支持 6 种内置主题和 5 种幻灯片类型。

## 触发词
生成PPT、制作PPT、创建演示文稿、PPT生成、pptx、powerpoint

## 技能文件

- **主脚本**: `skills/pptx-generator/generate_ppt.mjs`
- **依赖**: `skills/pptx-generator/package.json` (需要 `npm install`)

## 内置主题

| 主题名称 | 主色 | 辅色 | 适用场景 |
|---------|------|------|---------|
| Corporate | #1B365D | #4A90D9 | 商务汇报、企业演示 |
| Creative | #E67E22 | #2C3E50 | 创意提案、营销方案 |
| Minimal | #000000 | #666666 | 简约风格、学术报告 |
| Nature | #27AE60 | #2ECC71 | 环保、健康、教育 |
| Tech | #8E44AD | #1ABC9C | 科技、互联网、产品发布 |
| Elegant | #D4AF37 | #34495E | 高端品牌、颁奖典礼 |

## 支持的幻灯片类型

1. **标题页**: `{"type":"title","title":"主标题","subtitle":"副标题"}`
2. **内容页**: `{"type":"content","title":"标题","bullets":["要点1","要点2"]}`
3. **图文页**: `{"type":"image","title":"标题","imageUrl":"图片URL"}`
4. **表格页**: `{"type":"table","title":"标题","headers":["列1","列2"],"rows":[["行1列1","行1列2"]]}`
5. **图表页**: `{"type":"chart","title":"标题","chartType":"bar|line|pie","categories":["类别1"],"values":[100]}`

## 使用方式

当用户要求生成 PPT 时：

1. 确定 PPT 标题、主题和幻灯片内容
2. 构建 `--slides` 参数的 JSON 字符串（每个幻灯片一个）
3. 调用 Python 执行 Node.js 脚本
4. 返回生成的 PPT 文件路径

## 示例

### 基本用法
```python
import subprocess

# 生成一个包含标题页和内容页的 PPT
subprocess.run([
    "node", "skills/pptx-generator/generate_ppt.mjs",
    "--title", "2024年度总结",
    "--theme", "Corporate",
    "--output", "reports/pptx/2024-summary.pptx",
    "--slides", '{"type":"title","title":"2024年度总结","subtitle":"汇报人：张三"}',
    "--slides", '{"type":"content","title":"工作成果","bullets":["完成项目A","完成项目B","完成项目C"]}'
])
```

### 查看可用主题
```bash
node skills/pptx-generator/generate_ppt.mjs --list-themes
```

### 查看帮助
```bash
node skills/pptx-generator/generate_ppt.mjs --help
```

## 安装依赖

```bash
cd skills/pptx-generator
npm install
```

## 注意事项

- 输出文件保存在 `reports/` 目录
- 需要 Node.js 22+ 和 npm
- 中文使用 Microsoft YaHei 字体
- 如果用户没有指定主题，推荐 Corporate 主题
