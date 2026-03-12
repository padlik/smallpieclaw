***Setup Instructions***
* Install dependencies:
```
pip install -r requirements.txt
```
* Set environment variables:
```
export TELEGRAM_TOKEN="your_bot_token"
export ALLOWED_USERS="123456789,987654321"  # Telegram user IDs
export ADMIN_USER="123456789"
export LLM_PROVIDER="openai"  # or openrouter, anthropic, google
export LLM_API_KEY="your_api_key"
export LLM_MODEL="gpt-3.5-turbo"  # or gpt-4, claude-3-sonnet, etc.
```
* Run the agent:
```
python main.py
```

