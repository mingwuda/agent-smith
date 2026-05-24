# 日报生成助手

## Description
自动生成结构化日报，按项目分类今日工作

## Trigger
写日报、今日总结、生成日报、每日汇报

## Instructions
当用户要求生成日报时，按以下步骤执行：

1. 首先询问用户今天完成了哪些工作，如果没有主动提供的话
2. 将工作内容按项目分类整理（每个项目一个二级标题）
3. 每个项目下列出具体完成事项，标注状态：✅ 已完成 / 🔄 进行中 / ⛔ 阻塞
4. 如有阻塞项，列出原因和需要的支持
5. 如果用户有明日计划，在末尾添加「明日计划」板块
6. 生成 markdown 格式的日报文件，保存到工作区的 reports/ 目录
7. 文件命名格式：daily-report-YYYY-MM-DD.md

## Tools Required
file_write、list_files

## Environment Variables
REPORTS_DIR=reports
