# SolBot

A Python bot that monitors for new Jupiter-verified tokens on Solana, attempts to purchase them through the Trojan bot, and tracks purchase attempts with retry logic. This is an experimental project and should not be used in production. Always do your own research and use at your own risk. This is not financial advice.

## Prerequisites

Before setting up the SolBot, you need to:

1. **Connect with Trojan Bot**:
   - Open Telegram and message [@solana_trojanbot](https://t.me/solana_trojanbot)
   - Send `/start` to initialize the bot
   - Follow the bot's instructions to set up your wallet

2. **Create Telegram API Application**:
   - Visit [my.telegram.org/apps](https://my.telegram.org/apps)
   - Log in with your phone number
   - Create a new application if you haven't already
   - Note down your `api_id` and `api_hash`

3. **Get Your Telegram User ID**:
   - Message [@userinfobot](https://t.me/userinfobot) on Telegram
   - Forward any message to it
   - Note down the ID number it gives you

### Installation

The SolBot requires Python 3.11+ and some system dependencies:

```bash
# Install python 3.11
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv

# Install PM2 if not already installed
if command -v pm2 &> /dev/null
then
    pm2 startup && pm2 save --force
else
    sudo apt install jq npm -y
    sudo npm install pm2 -g && pm2 update
    npm install pm2@latest -g && pm2 update && pm2 save --force && pm2 startup && pm2 save
fi
```

Then clone the repository:

```bash
cd $HOME
git clone https://github.com/sirouk/solbot
cd ./solbot
```

Make a python virtual environment and install the dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

You should now have all required dependencies from the `pyproject.toml`.

If you need to cleanup and reinstall packages:

```bash
cd $HOME/solbot
deactivate;
rm -rf .venv
```

## Configuration

On first run, the bot will guide you through setting up your `.env` file. You'll need:
- Your Telegram API ID
- Your Telegram API Hash
- Your phone number (with country code)
- Your Telegram User ID

The bot will create a `.env` file with this information.


## Running the SolWatch
The SolWatch is a separate service that monitors the Solana blockchain for new tokens and saves them to a SQLite database. The SolBot will use this database to track tokens and purchase them.

### Setting up PM2 Service for SolWatch

Set up the SolWatch as a PM2 service:

```bash
# Start the SolWatch with PM2
pm2 start sol_ws.py --name SolWatch --interpreter python3

# Ensure PM2 starts on system boot
pm2 startup && pm2 save --force
```


## Running the SolBot

First, run manually to verify everything is working:

```bash
cd $HOME/solbot
source .venv/bin/activate
python3 main.py
```

### Setting up PM2 Service for SolBot

Once verified, set up the SolBot as a PM2 service:

```bash
# Start the SolBot with PM2
pm2 start main.py --name SolBot --interpreter python3 -- --auto

# Ensure PM2 starts on system boot
pm2 startup && pm2 save --force
```

### PM2 Log Management

Set up log rotation for the SolBot:

```bash
# Install pm2-logrotate module if not already installed
pm2 install pm2-logrotate

# Set maximum size of logs to 50M before rotation
pm2 set pm2-logrotate:max_size 50M

# Retain 10 rotated log files
pm2 set pm2-logrotate:retain 10

# Enable compression of rotated logs
pm2 set pm2-logrotate:compress true

# Set rotation interval to every 6 hours
pm2 set pm2-logrotate:rotateInterval '00 */6 * * *'
```

### Useful PM2 Commands

```bash
# View logs
pm2 logs SolBot

# Monitor processes
pm2 monit

# Restart the SolBot
pm2 restart SolBot

# Stop the SolBot
pm2 stop SolBot
```

## Features

- Monitors for new Jupiter-verified tokens
- Attempts to purchase tokens through Trojan bot
- Retries failed purchases up to 3 times
- Tracks all communications in SQLite database
- Sends notifications to your Telegram account
- Persists state between restarts

## TODO

Future improvements planned:
1. Integrate with additional data sources:
   - Monitor PumpFun for newly minted tokens
   - Fetch on-chain data for new token mints
2. Add token analysis:
   - Send tokens to [@ttfbotbot](https://t.me/ttfbotbot) for analysis
   - Parse responses to evaluate token safety
   - Implement automated decision making based on analysis results 