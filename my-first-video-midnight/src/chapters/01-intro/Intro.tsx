import { MaskReveal } from "../../components/MaskReveal";
import type { ChapterStepProps } from "../../registry/types";
import "./Intro.css";

export default function IntroChapter({ step }: ChapterStepProps) {
  /* Step 0 — 标题页 */
  if (step === 0) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-cover">
          <div className="intro-masthead">
            <span className="intro-brand">AgentSmith</span>
            <span className="intro-issue">本地 AI 智能体</span>
          </div>
          <hr className="rule" style={{ marginTop: "var(--space-5)" }} />
          <div className="intro-cover-body">
            <h1 className="intro-cover-h">
              <MaskReveal show duration={1000}>
                <span className="serif-cn">AgentSmith</span>
              </MaskReveal>
            </h1>
            <div className="intro-cover-foot label-mono">
              <span className="dot-accent" /> &nbsp;本地部署 · 私有可控 · 可扩展
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 1 — 项目定位 */
  if (step === 1) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-position">
          <div className="kicker">定位</div>
          <h2 className="intro-position-h">
            <MaskReveal show duration={900}>
              <span className="serif-cn">这不是聊天机器人，</span>
            </MaskReveal>
            <br />
            <MaskReveal show delay={400} duration={900}>
              <span className="serif-it intro-em">而是你的 AI 工作台</span>
            </MaskReveal>
          </h2>
          <p className="intro-position-p">
            所有数据都在你自己的机器上，不依赖第三方云服务。
            你可以把它部署在个人电脑上，也可以放在团队内网里，给所有人用。
          </p>
        </div>
      </div>
    );
  }

  /* Step 2 — 技术栈总览 */
  if (step === 2) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-tech">
          <div className="kicker">技术栈</div>
          <h2 className="intro-tech-h">FastAPI · LangGraph · OpenAI 兼容</h2>
          <div className="intro-tech-tags">
            <span className="intro-tag">FastAPI</span>
            <span className="intro-tag">LangGraph</span>
            <span className="intro-tag">OpenAI 兼容</span>
            <span className="intro-tag">Web UI</span>
          </div>
        </div>
      </div>
    );
  }

  /* Step 3 — 项目架构 */
  if (step === 3) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-arch">
          <div className="kicker">项目架构</div>
          <h2 className="intro-arch-h">分层架构，从用户到模型</h2>
          <div className="intro-arch-diagram">
            <div className="intro-arch-layer">
              <div className="intro-arch-layer-title">用户层</div>
              <div className="intro-arch-node intro-arch-user">Web UI</div>
              <div className="intro-arch-node intro-arch-wechat">微信 Bot</div>
            </div>
            <div className="intro-arch-arrow">
              <svg viewBox="0 0 80 24" className="intro-arch-svg">
                <path d="M0 12 L60 12" className="intro-arch-line" />
                <polygon points="60,6 80,12 60,18" className="intro-arch-head" />
              </svg>
            </div>
            <div className="intro-arch-layer">
              <div className="intro-arch-layer-title">接入层</div>
              <div className="intro-arch-node intro-arch-fastapi">FastAPI</div>
            </div>
            <div className="intro-arch-arrow">
              <svg viewBox="0 0 80 24" className="intro-arch-svg">
                <path d="M0 12 L60 12" className="intro-arch-line" />
                <polygon points="60,6 80,12 60,18" className="intro-arch-head" />
              </svg>
            </div>
            <div className="intro-arch-layer">
              <div className="intro-arch-layer-title">推理层</div>
              <div className="intro-arch-node intro-arch-langgraph">LangGraph</div>
            </div>
            <div className="intro-arch-arrow">
              <svg viewBox="0 0 80 24" className="intro-arch-svg">
                <path d="M0 12 L60 12" className="intro-arch-line" />
                <polygon points="60,6 80,12 60,18" className="intro-arch-head" />
              </svg>
            </div>
            <div className="intro-arch-layer">
              <div className="intro-arch-layer-title">模型层</div>
              <div className="intro-arch-node intro-arch-model">OpenAI 兼容模型</div>
            </div>
          </div>
          <div className="intro-arch-tools">
            <div className="intro-arch-tools-title">工具层</div>
            <div className="intro-arch-tool-list">
              <span className="intro-arch-tool">文件操作</span>
              <span className="intro-arch-tool">代码执行</span>
              <span className="intro-arch-tool">网页搜索</span>
              <span className="intro-arch-tool">Git 管理</span>
              <span className="intro-arch-tool">长期记忆</span>
              <span className="intro-arch-tool">微信消息</span>
            </div>
          </div>
          <p className="intro-arch-p">用户请求经 FastAPI 进入 LangGraph 状态机，按需调用工具或模型，最终返回结构化响应。</p>
        </div>
      </div>
    );
  }

  /* Step 4 — 前端体验 */
  if (step === 4) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-frontend">
          <div className="kicker">前端体验</div>
          <div className="intro-mock-ui">
            <div className="intro-mock-header">
              <span className="intro-mock-dot" />
              <span className="intro-mock-title">AgentSmith</span>
            </div>
            <div className="intro-mock-body">
              <div className="intro-mock-user">用户：帮我列出工作区文件</div>
              <div className="intro-mock-ai">
                <div className="intro-mock-thinking">思考中...</div>
                <div className="intro-mock-tool">调用工具：list_files</div>
                <div className="intro-mock-result">返回 12 个文件...</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 5 — 模型兼容性 */
  if (step === 5) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-models">
          <div className="kicker">模型兼容</div>
          <h2 className="intro-models-h">支持任意 OpenAI 兼容模型</h2>
          <div className="intro-model-list">
            <span className="intro-model-item">DeepSeek</span>
            <span className="intro-model-item">通义千问</span>
            <span className="intro-model-item">MiMo</span>
            <span className="intro-model-item">OpenAI</span>
          </div>
        </div>
      </div>
    );
  }

  /* Step 6 — 能力矩阵 */
  if (step === 6) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-abilities">
          <div className="kicker">核心能力</div>
          <div className="intro-ability-grid">
            <div className="intro-ability-item">文件操作</div>
            <div className="intro-ability-item">代码执行</div>
            <div className="intro-ability-item">网页搜索</div>
            <div className="intro-ability-item">Git 管理</div>
            <div className="intro-ability-item">Skills 扩展</div>
            <div className="intro-ability-item">长期记忆</div>
            <div className="intro-ability-item">多用户隔离</div>
            <div className="intro-ability-item intro-ability-wechat">微信接入</div>
            <div className="intro-ability-item">图片输入</div>
            <div className="intro-ability-item">多模态切换</div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 7 — 微信 Bot 接入流程 */
  if (step === 7) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-wechat">
          <div className="kicker">微信 Bot 接入</div>
          <h2 className="intro-wechat-h">基于腾讯 iLink Bot API 接入微信</h2>
          <div className="intro-wechat-steps">
            <div className="intro-wechat-step">
              <div className="intro-wechat-step-num">01</div>
              <div className="intro-wechat-step-title">安装依赖</div>
              <div className="intro-wechat-step-desc">安装 qrcode 依赖，确保后端加载 wechat_bot 模块。</div>
            </div>
            <div className="intro-wechat-step">
              <div className="intro-wechat-step-num">02</div>
              <div className="intro-wechat-step-title">扫码登录</div>
              <div className="intro-wechat-step-desc">访问 /wechat/qrcode 页面，用手机微信扫码确认，Token 自动持久化到本地。</div>
            </div>
            <div className="intro-wechat-step">
              <div className="intro-wechat-step-num">03</div>
              <div className="intro-wechat-step-title">开始对话</div>
              <div className="intro-wechat-step-desc">在微信里给 Bot 发消息即可，回复会同步到 Web 会话列表，支持 /new、/list、/switch 指令。</div>
            </div>
          </div>
          <div className="intro-wechat-flow">
            <div className="intro-wechat-flow-node">微信用户</div>
            <div className="intro-wechat-flow-arrow">→</div>
            <div className="intro-wechat-flow-node">iLink Bot API</div>
            <div className="intro-wechat-flow-arrow">→</div>
            <div className="intro-wechat-flow-node">FastAPI 网关</div>
            <div className="intro-wechat-flow-arrow">→</div>
            <div className="intro-wechat-flow-node">LangGraph</div>
            <div className="intro-wechat-flow-arrow">→</div>
            <div className="intro-wechat-flow-node">模型响应</div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 8 — 文件与代码 */
  if (step === 8) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-code">
          <div className="kicker">文件与代码</div>
          <h2 className="intro-code-h">不只是读写文件，而是完整的文件工作台</h2>
          <div className="intro-code-grid">
            <div className="intro-code-card">
              <div className="intro-code-card-title">文件操作</div>
              <div className="intro-code-card-desc">读写、追加、删除、列出、搜索工作区文件；大文件只返回摘要和路径，避免撑爆上下文。</div>
            </div>
            <div className="intro-code-card">
              <div className="intro-code-card-title">文件制品</div>
              <div className="intro-code-card-desc">AI 生成文件后自动追加下载链接；Markdown 文件支持弹窗预览，不用再手动打开。</div>
            </div>
            <div className="intro-code-card">
              <div className="intro-code-card-title">ZIP 上传</div>
              <div className="intro-code-card-desc">上传 ZIP 压缩包，自动解压到工作区并生成文件清单，供 AI 分析项目结构。</div>
            </div>
            <div className="intro-code-card">
              <div className="intro-code-card-title">Python 执行</div>
              <div className="intro-code-card-desc">运行 Python 代码并返回输出；超大输出只返回摘要、开头和结尾；支持实时流式输出。</div>
            </div>
          </div>
          <div className="intro-code-block">
            <div className="intro-code-line">
              <span className="intro-code-comment"># 读写文件</span>
            </div>
            <div className="intro-code-line">
              <span className="intro-code-keyword">with</span> open(<span className="intro-code-string">"data.txt"</span>) <span className="intro-code-keyword">as</span> f:
            </div>
            <div className="intro-code-line">
              &nbsp;&nbsp;content = f.read()
            </div>
            <div className="intro-code-line">
              <span className="intro-code-comment"># 运行 Python</span>
            </div>
            <div className="intro-code-line">
              result = run_python(<span className="intro-code-string">"print('hello')"</span>)
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 9 — Skills 扩展 */
  if (step === 9) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-skills">
          <div className="kicker">Skills 扩展</div>
          <h2 className="intro-skills-h">用 Skills 给 Agent 加载新能力</h2>
          <div className="intro-skills-desc">
            Skills 是轻量级能力包，基于 <span className="intro-code-keyword">SKILL.md</span> 描述文件，兼容 YAML frontmatter 和主流技能格式。
            内置 Skills 可直接使用，你也可以在 <span className="intro-code-string">~/.desktop_agent/skills/</span> 下添加自定义 Skills。
          </div>
          <div className="intro-skill-list">
            <div className="intro-skill-card">
              <div className="intro-skill-name">database-interaction</div>
              <div className="intro-skill-desc">自然语言查询数据库，支持 SQLite / PostgreSQL / MySQL，带列级/行级权限控制。</div>
            </div>
            <div className="intro-skill-card">
              <div className="intro-skill-name">agnes-image</div>
              <div className="intro-skill-desc">文生图 / 图生图，调用 Agnes Image API 生成高质量图片。</div>
            </div>
            <div className="intro-skill-card">
              <div className="intro-skill-name">pptx-generator</div>
              <div className="intro-skill-desc">用 Node.js + PptxGenJS 生成高质量 PowerPoint，支持 6 种主题和 5 种幻灯片类型。</div>
            </div>
            <div className="intro-skill-card">
              <div className="intro-skill-name">brainstorming</div>
              <div className="intro-skill-desc">需求澄清与设计讨论，适合在写代码前先理清思路。</div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 10 — 多用户与记忆 */
  if (step === 10) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-multi">
          <div className="kicker">多用户与记忆</div>
          <h2 className="intro-multi-h">每个用户都有独立的工作空间</h2>
          <div className="intro-multi-grid">
            <div className="intro-multi-card">
              <div className="intro-multi-card-title">数据隔离</div>
              <div className="intro-multi-card-desc">
                会话、用量、记忆、工作区全部按用户隔离存储。<br />
                <span className="intro-code-string">~/.desktop_agent/users/{user_id}/</span>
              </div>
            </div>
            <div className="intro-multi-card">
              <div className="intro-multi-card-title">长期记忆</div>
              <div className="intro-multi-card-desc">
                按用户隔离保存偏好、项目事实和常用环境信息；超过 10 天自动清理。
              </div>
            </div>
            <div className="intro-multi-card">
              <div className="intro-multi-card-title">管理员能力</div>
              <div className="intro-multi-card-desc">
                只有 admin 用户能看到设置和用户管理入口；支持批量配置用户列表。
              </div>
            </div>
            <div className="intro-multi-card">
              <div className="intro-multi-card-title">免密登录</div>
              <div className="intro-multi-card-desc">
                支持生成短期免密登录链接，可设置二维码和过期时间，方便临时访客。
              </div>
            </div>
          </div>
          <div className="intro-multi-diagram">
            <div className="intro-user-node">Web 用户 A</div>
            <div className="intro-user-node">Web 用户 B</div>
            <div className="intro-user-node intro-user-wechat">微信用户</div>
            <div className="intro-memory-bar">
              <div className="intro-memory-label">长期记忆</div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 11 — 适用场景 */
  if (step === 11) {
    return (
      <div className="intro-scene scene-pad">
        <div className="intro-scenes">
          <div className="kicker">适用场景</div>
          <div className="intro-scene-cards">
            <div className="intro-scene-card">
              <div className="intro-scene-card-title">个人知识助理</div>
              <div className="intro-scene-card-desc">
                本地部署，数据不出机器。用来整理笔记、搜索资料、运行脚本、管理个人项目。
              </div>
            </div>
            <div className="intro-scene-card">
              <div className="intro-scene-card-title">团队内网助手</div>
              <div className="intro-scene-card-desc">
                多用户隔离，每人独立工作区。团队共享内网部署，统一管理 Git、数据库和文档生成。
              </div>
            </div>
            <div className="intro-scene-card">
              <div className="intro-scene-card-title">开发者工具链</div>
              <div className="intro-scene-card-desc">
                直接操作文件系统、运行代码、搜索网页、管理 Git。Skills 扩展让能力持续生长。
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* Step 12 — 结尾 */
  return (
    <div className="intro-scene scene-pad">
      <div className="intro-end">
        <div className="kicker">开源可用</div>
        <h2 className="intro-end-h">
          <MaskReveal show duration={1000}>
            <span className="serif-cn">想深入了解哪个模块？</span>
          </MaskReveal>
        </h2>
        <div className="intro-end-cursor">_</div>
      </div>
    </div>
  );
}
