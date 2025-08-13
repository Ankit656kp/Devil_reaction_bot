<h1 align="center">
  <img src="https://readme-typing-svg.demolab.com?font=Monoton&size=40&duration=3000&pause=500&color=FF0000,FF7F00,FFFF00,00FF00,0000FF,4B0082,9400D3&center=true&vCenter=true&width=800&lines=🌈+Telegram+Broadcast+%26+Auto+Reaction+Bot;⚡+Multi+Group+%7C+Channel+Support;🚀+Fast+%7C+MongoDB+%7C+Heroku" alt="Typing SVG" />
</h1>

<p align="center">
  <b>Developer:</b> <a href="https://t.me/Ankitgupta214">🖤 Devil [@Ankitgupta124]</a>
</p>

---

## 🚀 Features
- 📢 **Broadcast** messages to all added groups/channels.
- 🤖 **Auto Reaction** to every message in chats & channels.
- 📊 **Stats** for sent/failed broadcast counts.
- 👑 **Admin & Owner Control** for secure access.
- 📥 **Join Groups/Channels** by link & store in DB.
- ⚡ **Fast, Secure & MongoDB Powered**.

---

## 🔧 Environment Variables
| Variable    | Description |
|-------------|-------------|
| `API_ID`    | From [my.telegram.org](https://my.telegram.org) |
| `API_HASH`  | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `MONGO_URI` | MongoDB connection string |
| `OWNER_IDS` | Space-separated Telegram User IDs of bot owners |

---

## 📜 Commands List

### 👑 Owner Commands
| Command       | Description |
|---------------|-------------|
| `/addgc <link>` | Add group/channel by invite link. |
| `/removegc <id>` | Remove group/channel from DB. |
| `/blocklist <id>` | Block group/channel from broadcasts. |
| `/unblocklist <id>` | Unblock group/channel. |
| `/broadcast <text>` | Send message to all allowed groups/channels. |
| `/addadmin <user_id>` | Add an admin who can broadcast. |
| `/removeadmin <user_id>` | Remove admin. |
| `/stats` | Show broadcast statistics (success/fail count). |
| `/ping` | Bot ping time (latency check). |

---

### 🛡 Admin Commands
| Command       | Description |
|---------------|-------------|
| `/broadcast <text>` | Send message to all allowed groups/channels. |
| `/stats` | Show own broadcast stats. |
| `/ping` | Check bot response speed. |

---

### 👤 Normal User Commands
| Command       | Description |
|---------------|-------------|
| `/start` | Shows bot intro & contact owner button. |
| `/help` | Get help & usage info. |

---

## 🛠 Deploy to Heroku
Click the button below to deploy your own bot instantly:

<p align="center">
  <a href="https://heroku.com/deploy?template=https://github.com/YourUsername/YourRepoName">
    <img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy to Heroku"/>
  </a>
</p>

---

## 📜 License
MIT License
