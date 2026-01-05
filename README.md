# Gmail Newsletter Summarizer

A Python script that automatically fetches today's newsletters from your Gmail 'Newsletters' label, summarizes each with Google Gemini, and emails you a daily digest.

## Features

- Fetches emails from Gmail 'Newsletters' label for today's date
- Summarizes each newsletter using Google Gemini
- Creates a markdown digest file
- Emails the digest to you automatically
- Handles OAuth2 authentication with automatic token refresh

## Prerequisites

- Python 3.8 or higher
- A Gmail account with newsletters labeled as 'Newsletters'
- Google Cloud Project with Gmail API enabled
- Google Gemini API key

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Gmail API Credentials Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Gmail API:
   - Navigate to "APIs & Services" > "Library"
   - Search for "Gmail API"
   - Click "Enable"
4. Create OAuth 2.0 credentials:
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "OAuth client ID"
   - If prompted, configure the OAuth consent screen:
     - Choose "External" (unless you have a Google Workspace)
     - Fill in the required fields (App name, User support email, etc.)
     - Add your email to test users
   - Application type: Choose "Desktop app"
   - Name it (e.g., "Gmail Newsletter Summarizer")
   - Click "Create"
5. Download the credentials:
   - Click the download icon next to your newly created OAuth client
   - Save the file as `credentials.json` in this project directory

### 3. Google Gemini API Key Setup

1. Get your API key from [Google AI Studio](https://makersuite.google.com/app/apikey) or [Google Cloud Console](https://console.cloud.google.com/)
2. Set it as an environment variable:

   **Linux/Mac:**
   ```bash
   export GEMINI_API_KEY='your-api-key-here'
   ```

   **Windows (PowerShell):**
   ```powershell
   $env:GEMINI_API_KEY='your-api-key-here'
   ```

   **Windows (Command Prompt):**
   ```cmd
   set GEMINI_API_KEY=your-api-key-here
   ```

   Alternatively, you can add it to your shell profile (`.bashrc`, `.zshrc`, etc.) to make it persistent.

### 4. Configure Recipient Email (Optional)

By default, the script will send the digest to the authenticated Gmail account. To send to a different email:

**Linux/Mac:**
```bash
export RECIPIENT_EMAIL='your-email@example.com'
```

**Windows (PowerShell):**
```powershell
$env:RECIPIENT_EMAIL='your-email@example.com'
```

## Usage

### First Run

1. Make sure `credentials.json` is in the project directory
2. Set your `GEMINI_API_KEY` environment variable
3. Run the script:

```bash
python gmail_summarizer.py
```

4. On first run, a browser window will open asking you to:
   - Sign in to your Google account
   - Grant permissions for Gmail read and send access
   - Click "Allow"
5. After authorization, a `token.json` file will be created (this stores your credentials for future runs)

### Subsequent Runs

Simply run:

```bash
python gmail_summarizer.py
```

The script will:
1. Authenticate using the stored token
2. Fetch today's emails from your 'Newsletters' label
3. Summarize each newsletter
4. Save a markdown file: `newsletter_digest_YYYY-MM-DD.md`
5. Email the digest to you

## Daily Automation

To run this script automatically every day:

### Linux/Mac (cron)

1. Open your crontab:
   ```bash
   crontab -e
   ```

2. Add a line to run the script daily (e.g., at 6 PM):
   ```bash
   0 18 * * * cd /path/to/Gmail-Newsletter-Summarizer && /usr/bin/python3 gmail_summarizer.py >> /path/to/logfile.log 2>&1
   ```

   Make sure to:
   - Replace `/path/to/Gmail-Newsletter-Summarizer` with your actual project path
   - Use the full path to `python3` (find it with `which python3`)
   - Set the `GEMINI_API_KEY` environment variable in your crontab or shell profile

### Windows (Task Scheduler)

1. Open Task Scheduler
2. Create a new task
3. Set trigger to "Daily" at your preferred time
4. Set action to start a program:
   - Program: `python.exe` (or full path to Python)
   - Arguments: `gmail_summarizer.py`
   - Start in: Your project directory
5. Add environment variable `GEMINI_API_KEY` in the task's environment variables

## File Structure

```
Gmail-Newsletter-Summarizer/
├── gmail_summarizer.py      # Main script
├── requirements.txt          # Python dependencies
├── README.md              # This file
├── credentials.json        # Gmail API credentials (you download this)
├── token.json             # OAuth token (auto-generated on first run)
└── newsletter_digest_*.md  # Generated digest files
```

## Configuration

You can modify these constants in `gmail_summarizer.py`:

- `LABEL_NAME`: Change from 'Newsletters' to your preferred label name
- `GEMINI_MODEL`: Change from 'gemini-pro' to other Gemini models (e.g., 'gemini-pro-vision')
- `CREDENTIALS_FILE`: Path to your credentials file (default: 'credentials.json')
- `TOKEN_FILE`: Path to store OAuth token (default: 'token.json')

## Troubleshooting

### "credentials.json not found"
- Make sure you've downloaded the OAuth credentials from Google Cloud Console
- Ensure the file is named exactly `credentials.json` and is in the project directory

### "GEMINI_API_KEY environment variable not set"
- Verify you've set the environment variable in your current shell session
- For cron jobs, ensure the environment variable is set in your crontab or shell profile

### "No newsletters found for today"
- Check that you have emails in your 'Newsletters' label
- Verify the label name matches exactly (case-sensitive)
- Ensure the emails were received today (the script filters by today's date)

### OAuth token expired
- Delete `token.json` and run the script again to re-authenticate
- The script should automatically refresh tokens, but if issues persist, delete and re-authenticate

### Email sending fails
- Verify you granted "send" permission during OAuth consent
- Check that the recipient email is valid
- Ensure your Gmail account has sending enabled

## Notes

- The script only processes emails received today (based on your system's date)
- Summaries are limited to the first 4000 characters of each email to manage token usage
- The markdown file is saved locally for archival purposes
- The email body contains the digest in plain text format (markdown formatting is simplified)

## License

This script is provided as-is for personal use.

