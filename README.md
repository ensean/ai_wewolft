# AI 狼人杀

让不同的 AI 模型互相博弈的狼人杀游戏。每位玩家可配置不同的 LLM，游戏完全自主运行，浏览器实时观看。

## 功能

- 支持 **AWS Bedrock**（Claude、Nova、MiniMax、DeepSeek、Kimi K2、Llama 4 等）
- 支持第三方 **OpenAI 兼容 API**（Kimi、DeepSeek、MiniMax、GLM）
- 每位玩家独立配置模型，同一局可混用不同厂商
- 游戏完全自主运行，所有发言、投票、技能均由 LLM 决策
- 实时 SSE 推流，浏览器逐条显示发言、投票、死亡事件

## 角色

| 角色 | 说明 |
|------|------|
| 🐺 狼人 | 夜晚合谋击杀，白天伪装好人 |
| 👨 村民 | 通过推理找出狼人 |
| 🔮 预言家 | 每夜查验一名玩家的阵营 |
| 🧪 女巫 | 持有一瓶解药和一瓶毒药 |
| 🔫 猎人 | 死亡时可带走一名玩家 |

## 快速开始

```bash
# 1. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 AWS 凭证（Bedrock 模型需要）
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_DEFAULT_REGION=us-east-1

# 4. 启动服务器
uvicorn backend.main:app --reload --port 8000

# 5. 打开浏览器
open http://localhost:8000
```

## 使用方式

1. 打开页面后填写玩家人数和 AWS Region
2. 点击「🔄 加载 Bedrock 模型」从 AWS 获取最新可用模型列表
3. 为每位玩家配置名字和模型；使用第三方 API 的玩家在 API Key 列填写对应密钥
4. 点击「开始游戏」，在右侧实时观看对局

## 项目结构

```
ai_wolf/
├── backend/
│   ├── main.py                 # FastAPI 入口
│   ├── game/
│   │   ├── state.py            # 游戏状态与数据模型
│   │   ├── engine.py           # 游戏主循环、角色分配
│   │   └── phases.py           # 夜晚/白天完整流程
│   ├── ai/
│   │   ├── bedrock_client.py   # AWS Bedrock Converse API
│   │   ├── openai_client.py    # OpenAI 兼容 API（Kimi/DeepSeek/GLM/MiniMax）
│   │   ├── player_agent.py     # AI 玩家（speak/vote/角色技能）
│   │   └── prompts.py          # 各角色中文 Prompt 模板
│   └── api/
│       ├── bedrock_models.py   # 动态获取 Bedrock 模型列表
│       ├── routes.py           # REST + SSE 路由
│       └── sse.py              # 实时事件推送
└── frontend/
    ├── index.html              # 配置页 + 游戏日志
    ├── style.css
    └── app.js
```

## 支持的模型

### AWS Bedrock（动态加载）

点击「加载 Bedrock 模型」按钮后从 AWS 实时获取，包括：

- **Anthropic**：Claude Sonnet 4.6、Claude Opus 4.7、Claude Haiku 4.5 等（通过跨区推理 profile）
- **Amazon**：Nova Pro、Nova Lite、Nova Micro 等
- **DeepSeek**：DeepSeek V3.2、DeepSeek-R1
- **MiniMax**：MiniMax M2.5
- **Moonshot AI**：Kimi K2、Kimi K2.5
- **Meta**：Llama 4 Maverick、Llama 4 Scout 等
- 以及 Mistral、Qwen、NVIDIA、GLM 等

### 第三方 API（填写 API Key 使用）

| 服务 | 模型示例 |
|------|---------|
| Kimi | moonshot-v1-8k / 32k / 128k |
| DeepSeek | deepseek-chat、deepseek-reasoner |
| MiniMax | MiniMax-Text-01 |
| GLM | glm-4-flash、glm-4-air |
