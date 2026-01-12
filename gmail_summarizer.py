#!/usr/bin/env python3
"""
Gmail Newsletter Summarizer

Fetches recent emails from the 'Newsletters' label, summarizes each with Hugging Face,
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

from huggingface_hub import InferenceClient
from dateutil import tz


# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.send']

# Configuration
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
LABEL_NAME = 'Newsletters'
HF_MODEL = 'sshleifer/distilbart-cnn-12-6'  # Summarization model (faster than BART-large)


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


def summarize_email(client, subject, from_addr, body):
    """Summarize email content using Hugging Face API.
    
    For long emails, chunks the text and processes ~50% of characters (every other chunk).
    """
    
    # Model token limit is ~1024 tokens (~4000 chars), use 2000 to be safe
    MAX_LENGTH = 2000
    CHUNK_SIZE = 2000  # Characters per chunk
    OVERLAP = 200  # Overlap between chunks to preserve context
    
    def try_summarize(text_to_summarize):
        """Helper to summarize text and return summary or None."""
        try:
            response = client.summarization(text_to_summarize)
            
            # Extract summary from response
            if response and hasattr(response, 'summary_text'):
                return response.summary_text.strip()
            elif isinstance(response, dict) and 'summary_text' in response:
                return response['summary_text'].strip()
            elif isinstance(response, str):
                return response.strip()
            return None
        except Exception as e:
            return None
    
    def format_text(body_text):
        """Format text for summarization."""
        return f"""Newsletter Email

From: {from_addr}
Subject: {subject}

{body_text}"""
    
    try:
        # For short emails, summarize directly
        if len(body) <= MAX_LENGTH:
            print(f"    [Email length: {len(body)} chars, summarizing...]", end='', flush=True)
            text_to_summarize = format_text(body)
            summary = try_summarize(text_to_summarize)
            if summary:
                print(f" ✓ ({len(summary)} chars)")
                return summary
            print(f" ✗")
            return "Error: Could not generate summary"
        
        # For long emails, chunk and process every other chunk (~50% coverage)
        print(f"    [Long email ({len(body)} chars), chunking and processing ~50%...]")
        
        # Split into chunks with overlap
        chunks = []
        start = 0
        while start < len(body):
            end = min(start + CHUNK_SIZE, len(body))
            chunk = body[start:end]
            if chunk.strip():
                chunks.append(chunk)
            # Move to next chunk with overlap
            start = end - OVERLAP
            if start >= len(body):
                break
        
        if not chunks:
            return "Error: Could not split email into chunks"
        
        # Process every other chunk (0, 2, 4, 6, etc.) to get ~50% coverage
        selected_indices = list(range(0, len(chunks), 2))
        selected_chunk_nums = [i+1 for i in selected_indices]  # 1-indexed for display
        
        total_chars_processed = sum(len(chunks[i]) for i in selected_indices)
        coverage_pct = (total_chars_processed / len(body)) * 100
        
        print(f"    [Split into {len(chunks)} chunk(s), processing {len(selected_indices)} chunks ({coverage_pct:.0f}% coverage): {', '.join(map(str, selected_chunk_nums))}]")
        
        # Summarize selected chunks
        chunk_summaries = []
        for i, chunk_idx in enumerate(selected_indices):
            chunk = chunks[chunk_idx]
            chunk_num = chunk_idx + 1  # 1-indexed for display
            print(f"    [Chunk {chunk_num}/{len(chunks)}: Processing... ({len(chunk)} chars)]", end='', flush=True)
            text_to_summarize = format_text(chunk)
            summary = try_summarize(text_to_summarize)
            if summary:
                chunk_summaries.append(summary)
                print(f" ✓ ({len(summary)} chars)")
            else:
                print(f" ✗")
        
        # Combine all chunk summaries
        if chunk_summaries:
            combined = " ".join(chunk_summaries)
            print(f"    [Combined summary from {len(chunk_summaries)} chunks: {len(combined)} chars]")
            return combined
        else:
            return "Error: Could not generate any chunk summaries"
    
    except Exception as e:
        print(f" ✗ (error: {str(e)[:100]})")
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
    
    # Check for Hugging Face API key (optional - some models work without it)
    hf_api_key = os.getenv('HF_API_KEY') or os.getenv('HUGGINGFACE_API_KEY')
    
    # Initialize Hugging Face client
    # Note: Some models work without API key, but having one increases rate limits
    if hf_api_key:
        client = InferenceClient(model=HF_MODEL, token=hf_api_key)
    else:
        print("Warning: No HF_API_KEY set. Using public API (may have lower rate limits).")
        client = InferenceClient(model=HF_MODEL)
    
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
    print(f"\nSummarizing {len(emails)} newsletter(s) with Hugging Face...")
    summaries = []
    for i, email_data in enumerate(emails, 1):
        body_len = len(email_data['body'])
        print(f"\n  [{i}/{len(emails)}] {email_data['subject'][:50]}...")
        print(f"       Body length: {body_len} chars")
        summary = summarize_email(
            client,
            email_data['subject'],
            email_data['from'],
            email_data['body']
        )
        summaries.append(summary)
        print(f"       Summary length: {len(summary)} chars")
    
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

