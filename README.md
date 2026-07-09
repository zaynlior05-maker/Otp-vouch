# Telegram Mirror Pro

A production-ready Telegram mirroring bot built with **Telethon** and the **Telegram Bot API**.

## Features

- Monitor one Telegram source channel
- Mirror messages almost instantly
- Replace unlimited usernames
- Replace Telegram links automatically
- Send messages using your own utility bot
- Support:
  - Text
  - Stickers
- Ignore:
  - Photos
  - Videos
  - Voice Notes
  - Documents
  - Audio
- Railway Ready
- GitHub Ready
- Automatic reconnect
- FloodWait handling
- Logging
- Duplicate protection

---

# Project Structure

```
telegram-mirror/
│
├── bot.py
├── config.py
├── replace.json
├── requirements.txt
├── Procfile
├── runtime.txt
├── .env.example
├── README.md
└── .gitignore
```

---

# Requirements

Python 3.11+

---

# Installation

Clone your repository.

```
git clone https://github.com/YOUR_USERNAME/telegram-mirror.git
```

Open the project.

```
cd telegram-mirror
```

Install dependencies.

```
pip install -r requirements.txt
```

---

# Telegram API

Create an application:

https://my.telegram.org

Copy:

- API_ID
- API_HASH

---

# Create Session String

Generate a Telethon Session String and save it as

```
SESSION_STRING
```

inside Railway Variables.

---

# BotFather

Create your utility bot.

Copy the token.

```
BOT_TOKEN
```

---

# Bot Permissions

Your bot MUST be an administrator in the destination group.

Recommended permissions:

- Send Messages
- Send Stickers
- Embed Links

---

# Configure replace.json

Example

```json
{
  "usernames": {
    "@CoinTelegraph":"@YourChannel"
  },
  "links": {
    "https://t.me/CoinTelegraph":"https://t.me/YourChannel"
  },
  "text":{
    "CoinTelegraph":"Your Brand"
  }
}
```

Unlimited replacements are supported.

---

# Railway Variables

Add the following Variables.

```
API_ID=

API_HASH=

SESSION_STRING=

BOT_TOKEN=

SOURCE_CHANNEL=@sourcechannel

TARGET_CHAT=-100xxxxxxxxxx
```

---

# Deploy to Railway

1. Push project to GitHub.

2. Create a Railway Project.

3. Select

Deploy from GitHub.

4. Choose your repository.

5. Add Variables.

6. Deploy.

Railway will automatically execute

```
python bot.py
```

---

# Logging

The bot writes runtime information to stdout.

Railway automatically captures logs.

---

# Recovery

The bot automatically:

- reconnects
- retries on FloodWait
- survives temporary network failures

---

# Updating Username Replacements

Simply edit

```
replace.json
```

Restart Railway.

No code changes required.

---

# Security

Never upload

```
.env
```

Never share

- SESSION_STRING

- BOT_TOKEN

- API_HASH

---

# License

Private