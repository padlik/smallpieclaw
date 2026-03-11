# Lightweight Autonomous Telegram Agent for Raspberry Pi

A resource-efficient AI agent that runs on Raspberry Pi (2GB RAM) and interacts via Telegram. Features tool execution, semantic tool discovery, self-extending tools, scheduled tasks, and remote LLM reasoning.

## Features

- 🛠️ **Tool Execution**: Runs local bash/python scripts safely with timeouts
- 🔍 **Semantic Tool Discovery**: AI-powered tool selection via embeddings
- 🔧 **Self-Extending Tools**: Creates new tools when needed
- 📅 **Scheduled Tasks**: Background monitoring and maintenance
- 🤖 **Remote LLM Reasoning**: Uses external APIs for heavy computation
- 🔒 **Security**: Allow-list access control with pairing mechanism
- ⚡ **Low Resource**: Optimized for 2GB RAM Raspberry Pi

## Quick Start

1. **Install Dependencies**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt

