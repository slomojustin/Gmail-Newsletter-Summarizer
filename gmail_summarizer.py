#!/usr/bin/env python3
"""
Gmail Newsletter Summarizer

Fetches today's emails from the 'Newsletters' label, summarizes each with Google Gemini,
and emails a markdown digest to the user.
"""

import os
import base64
import json
import email
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google import genai
from dateutil import tz


# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.send']

# Configuration
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
LABEL_NAME = 'Newsletters'
GEMINI_MODEL = 'models/gemini-flash-latest'


def get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds = None
    
    # Load existing token
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"'{CREDENTIALS_FILE}' not found. Please download it from "
                    "Google Cloud Console and place it in this directory."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        # Save credentials for next run
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    
    return build('gmail', 'v1', credentials=creds)


def get_date_query(days_back=0):
    """Get Gmail query string for a specific date.
    
    Args:
        days_back: Number of days ago (0 = today, 1 = yesterday, 2 = day before)
    
    Returns:
        Gmail query string for that date
    """
    target_date = date.today() - timedelta(days=days_back)
    # Gmail uses YYYY/MM/DD format
    return f"after:{target_date.year}/{target_date.month:02d}/{target_date.day:02d} before:{(target_date + timedelta(days=1)).year}/{(target_date + timedelta(days=1)).month:02d}/{(target_date + timedelta(days=1)).day:02d}"


def get_recent_dates_query():
    """Get Gmail query string for today, yesterday, and day before."""
    day_before_yesterday = date.today() - timedelta(days=2)
    # Gmail query: after day_before_yesterday (includes today, yesterday, day before)
    return f"after:{day_before_yesterday.year}/{day_before_yesterday.month:02d}/{day_before_yesterday.day:02d}"


def extract_email_body(message):
    """Extract text body from email message."""
    body = ""
    
    if 'payload' in message:
        payload = message['payload']
        
        # Handle multipart messages
        if 'parts' in payload:
            for part in payload['parts']:
                mime_type = part.get('mimeType', '')
                if mime_type == 'text/plain':
                    data = part.get('body', {}).get('data', '')
                    if data:
                        body += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                elif mime_type == 'text/html' and not body:
                    # Fallback to HTML if no plain text
                    data = part.get('body', {}).get('data', '')
                    if data:
                        html_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                        # Simple HTML to text conversion (basic)
                        body += html_body
        else:
            # Single part message
            mime_type = payload.get('mimeType', '')
            if mime_type == 'text/plain':
                data = payload.get('body', {}).get('data', '')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            elif mime_type == 'text/html':
                data = payload.get('body', {}).get('data', '')
                if data:
                    html_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                    body = html_body
    
    return body.strip()


def get_email_headers(message):
    """Extract headers from email message."""
    headers = message.get('payload', {}).get('headers', [])
    header_dict = {}
    for header in headers:
        header_dict[header['name'].lower()] = header['value']
    return header_dict


def fetch_todays_newsletters(service):
    """Fetch recent emails (today, yesterday, day before) from Newsletters label."""
    query = f"label:{LABEL_NAME} {get_recent_dates_query()}"
    
    try:
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=50
        ).execute()
        
        messages = results.get('messages', [])
        
        if not messages:
            print(f"No newsletters found for recent days (today, yesterday, day before).")
            return []
        
        print(f"Found {len(messages)} newsletter(s) from recent days.")
        
        # Fetch full message details
        email_data = []
        for msg in messages:
            try:
                message = service.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='full'
                ).execute()
                
                headers = get_email_headers(message)
                body = extract_email_body(message)
                
                email_data.append({
                    'id': msg['id'],
                    'subject': headers.get('subject', '(No Subject)'),
                    'from': headers.get('from', 'Unknown'),
                    'date': headers.get('date', ''),
                    'body': body
                })
            except HttpError as error:
                print(f"Error fetching message {msg['id']}: {error}")
                continue
        
        return email_data
    
    except HttpError as error:
        print(f"Error fetching emails: {error}")
        return []


def summarize_email(client, model_name, subject, from_addr, body):
    """Summarize email content using Google Gemini."""
    # Limit body length to avoid token limits
    body_preview = body[:4000]
    
    prompt = f"""Please provide a concise summary of this newsletter email.

From: {from_addr}
Subject: {subject}

Content:
{body_preview}

Provide a brief summary (2-3 sentences) highlighting the key points."""
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=2000,  # Increased to avoid truncation
                temperature=0.7,
            )
        )
        
        # Check finish_reason to understand response status
        finish_reason = None
        if hasattr(response, 'candidates') and response.candidates and len(response.candidates) > 0:
            finish_reason = getattr(response.candidates[0], 'finish_reason', None)
            if finish_reason and str(finish_reason) == 'FinishReason.MAX_TOKENS':
                print(f"    [WARNING] Response hit MAX_TOKENS limit - may be incomplete")
        
        # Check finish_reason - if MAX_TOKENS, we might have a truncated or None response
        finish_reason = None
        if hasattr(response, 'candidates') and response.candidates and len(response.candidates) > 0:
            finish_reason = getattr(response.candidates[0], 'finish_reason', None)
            if finish_reason and str(finish_reason) == 'FinishReason.MAX_TOKENS':
                print(f"    [DEBUG] WARNING: Hit MAX_TOKENS - response may be incomplete")
        
        # Extract text from response
        # Primary: try response.text (most common)
        if hasattr(response, 'text') and response.text is not None:
            text = response.text.strip()
            if text:  # Make sure it's not empty
                # If we hit MAX_TOKENS, add a note
                if finish_reason and str(finish_reason) == 'FinishReason.MAX_TOKENS':
                    return text + " [Summary may be truncated due to length]"
                return text
        
        # Fallback: try candidates path
        if hasattr(response, 'candidates') and response.candidates and len(response.candidates) > 0:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content:
                # Check if parts exists and is not None
                if hasattr(candidate.content, 'parts'):
                    parts = candidate.content.parts
                    if parts is not None and len(parts) > 0:
                        part = parts[0]
                        if hasattr(part, 'text') and part.text:
                            text = part.text.strip()
                            if text:  # Make sure it's not empty
                                return text
        
        # If all else fails, return error message
        return f"Error: Could not extract text from response"
    
    except Exception as e:
        print(f"Error summarizing email: {e}")
        return f"Error generating summary: {str(e)}"


def create_markdown_digest(emails, summaries):
    """Create markdown digest from emails and summaries."""
    today = date.today()
    digest = f"# Newsletter Digest - {today.strftime('%Y-%m-%d')}\n\n"
    digest += f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
    digest += "---\n\n"
    
    for email_data, summary in zip(emails, summaries):
        digest += f"## {email_data['subject']}\n\n"
        digest += f"**From:** {email_data['from']}\n\n"
        digest += f"**Date:** {email_data['date']}\n\n"
        digest += f"**Summary:**\n{summary}\n\n"
        digest += "---\n\n"
    
    return digest


def send_email(service, recipient_email, subject, body):
    """Send email using Gmail API."""
    try:
        message = MIMEText(body)
        message['to'] = recipient_email
        message['subject'] = subject
        
        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode('utf-8')
        
        send_message = service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()
        
        print(f"Email sent successfully! Message ID: {send_message['id']}")
        return True
    
    except HttpError as error:
        print(f"Error sending email: {error}")
        return False


def get_recipient_email(service):
    """Get recipient email address (defaults to authenticated user's email)."""
    recipient = os.getenv('RECIPIENT_EMAIL')
    if recipient:
        return recipient
    
    # Get user's email from profile
    try:
        profile = service.users().getProfile(userId='me').execute()
        return profile.get('emailAddress', 'me')
    except HttpError:
        return 'me'  # Fallback to 'me' which Gmail API will resolve


def main():
    """Main function."""
    print("Gmail Newsletter Summarizer")
    print("=" * 40)
    
    # Check for Gemini API key
    gemini_api_key = os.getenv('GEMINI_API_KEY')
    if not gemini_api_key:
        raise ValueError(
            "GEMINI_API_KEY environment variable not set. "
            "Please set it with: export GEMINI_API_KEY=AIzaSyAG0j4Uhq1D_xiZqI26XTrSq02rdNFRGjU"
        )
    
    # Initialize Gemini model
    client = genai.Client(api_key=gemini_api_key)
    gemini_model = GEMINI_MODEL
    
    # Authenticate Gmail
    print("Authenticating with Gmail API...")
    service = get_gmail_service()
    print("Authentication successful!")
    
    # Fetch recent newsletters (today, yesterday, day before)
    print(f"\nFetching recent newsletters (today, yesterday, day before) from '{LABEL_NAME}' label...")
    emails = fetch_todays_newsletters(service)
    
    if not emails:
        print("No newsletters to process. Exiting.")
        return
    
    # Summarize each email
    print(f"\nSummarizing {len(emails)} newsletter(s) with Google Gemini...")
    summaries = []
    for i, email_data in enumerate(emails, 1):
        print(f"  [{i}/{len(emails)}] Summarizing: {email_data['subject'][:50]}...")
        summary = summarize_email(
            client,
            gemini_model,
            email_data['subject'],
            email_data['from'],
            email_data['body']
        )
        summaries.append(summary)
    
    # Create markdown digest
    print("\nCreating markdown digest...")
    digest = create_markdown_digest(emails, summaries)
    
    # Save to file
    today = date.today()
    filename = f"newsletter_digest_{today.strftime('%Y-%m-%d')}.md"
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(digest)
    print(f"Digest saved to: {filename}")
    
    # Send email
    print("\nSending email digest...")
    recipient = get_recipient_email(service)
    email_subject = f"Newsletter Digest - {today.strftime('%Y-%m-%d')}"
    
    # Convert markdown to plain text for email (simple conversion)
    email_body = digest.replace('**', '').replace('#', '').replace('---', '---')
    
    send_email(service, recipient, email_subject, email_body)
    
    print("\nDone!")


if __name__ == '__main__':
    main()

