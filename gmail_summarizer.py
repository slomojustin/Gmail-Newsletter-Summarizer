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
import re
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
import requests
from bs4 import BeautifulSoup
import html2text


# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.send']

# Configuration
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
LABEL_NAME = 'Newsletters'
HF_MODEL = 'facebook/bart-large-cnn'  # Summarization model (high quality)


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


def get_todays_date_query():
    """Get Gmail query string for today's date only."""
    today = date.today()
    # Gmail query: after today and before tomorrow (only today's emails)
    tomorrow = today + timedelta(days=1)
    return f"after:{today.year}/{today.month:02d}/{today.day:02d} before:{tomorrow.year}/{tomorrow.month:02d}/{tomorrow.day:02d}"


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
    """Fetch today's emails from Newsletters label."""
    query = f"label:{LABEL_NAME} {get_todays_date_query()}"
    
    try:
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=50
        ).execute()
        
        messages = results.get('messages', [])
        
        if not messages:
            print(f"No newsletters found for today.")
            return []
        
        print(f"Found {len(messages)} newsletter(s) from today.")
        
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


def extract_urls(text):
    """Extract the main article URL from text (for Substack newsletters).
    
    Looks for the main Substack article link. Substack emails often have redirect links
    that point to the main article, so we look for the first substantial Substack link.
    """
    # Pattern to match URLs
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text)
    
    # Find Substack URLs
    substack_urls = [url for url in urls if 'substack.com' in url.lower()]
    
    if not substack_urls:
        return []
    
    # Strategy 1: Look for direct article links (format: https://[username].substack.com/p/[slug])
    direct_article_urls = []
    for url in substack_urls:
        url_lower = url.lower()
        # Must have /p/ (article path)
        if '/p/' in url:
            # Must NOT be a redirect link
            if 'redirect' not in url_lower:
                # Must be from a specific subdomain (not just substack.com)
                if '.substack.com' in url:
                    direct_article_urls.append(url)
    
    if direct_article_urls:
        # Return the first direct article URL (usually the main one)
        return [direct_article_urls[0]]
    
    # Strategy 2: If no direct links, try to extract from redirect links
    # Substack redirect links often contain the target URL in the redirect parameter
    for url in substack_urls:
        if 'redirect' in url.lower():
            # Try to extract the target URL from redirect parameter
            # Format: https://substack.com/redirect/...?url=ENCODED_URL
            match = re.search(r'[?&]url=([^&]+)', url)
            if match:
                # URL is usually base64 encoded or URL encoded
                try:
                    import urllib.parse
                    decoded = urllib.parse.unquote(match.group(1))
                    # Check if it's a Substack article URL
                    if 'substack.com' in decoded and '/p/' in decoded:
                        return [decoded]
                except:
                    pass
    
    # Strategy 3: Return the first Substack URL that looks like an article
    # (even if it's a redirect, we'll try to follow it)
    for url in substack_urls:
        if '/p/' in url or 'substack.com/p/' in url.lower():
            return [url]
    
    # Fallback: return first Substack URL
    return [substack_urls[0]] if substack_urls else []


def fetch_article_content(url):
    """Fetch and extract article content from a URL, following redirects."""
    try:
        # Set headers to mimic a browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Follow redirects to get the final URL
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        response.raise_for_status()
        
        # If we got redirected, use the final URL
        final_url = response.url
        
        # Parse HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # For Substack articles, try to find the article content
        # Substack typically uses <div class="post"> or similar
        article_content = None
        
        # Try Substack-specific selectors
        substack_selectors = [
            'div[class*="post"]',
            'article',
            'div[class*="content"]',
            'div[class*="body"]',
            'div[class*="article"]'
        ]
        
        for selector in substack_selectors:
            article = soup.select_one(selector)
            if article:
                article_content = article
                break
        
        # Fallback to body if no article found
        if not article_content:
            article_content = soup.find('body')
        
        if not article_content:
            return None
        
        # Convert HTML to text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0  # Don't wrap lines
        
        text = h.handle(str(article_content))
        
        # Clean up the text
        text = re.sub(r'\n\s*\n', '\n\n', text)  # Remove excessive newlines
        text = text.strip()
        
        return text if text else None
        
    except Exception as e:
        print(f"        Error fetching {url}: {str(e)[:80]}")
        return None


def summarize_email(client, subject, from_addr, body):
    """Summarize email content by taking first 3 chunks, summarizing each, then creating a connected final summary."""
    
    MAX_LENGTH = 2000  # For short emails, summarize directly
    CHUNK_SIZE = 1500  # Size of each chunk
    CHUNK_SUMMARY_LENGTH = 300  # Chunk summaries should be longer for better final summary
    MAX_SUMMARY_LENGTH = 600  # Cap final combined summary length
    
    def format_text(content_text, section=""):
        """Format text for summarization."""
        section_label = f" ({section})" if section else ""
        return f"""Email{section_label}

From: {from_addr}
Subject: {subject}

{content_text}"""
    
    def try_summarize(text_to_summarize, max_length=None):
        """Helper to summarize text and return summary or None."""
        try:
            response = client.summarization(text_to_summarize)
            
            # Extract summary from response
            if response and hasattr(response, 'summary_text'):
                summary = response.summary_text.strip()
            elif isinstance(response, dict) and 'summary_text' in response:
                summary = response['summary_text'].strip()
            elif isinstance(response, str):
                summary = response.strip()
            else:
                return None
            
            # Cap summary length (use provided max_length or default)
            cap_length = max_length if max_length else MAX_SUMMARY_LENGTH
            if len(summary) > cap_length:
                summary = summary[:cap_length].rsplit('.', 1)[0] + '.'
            
            return summary
        except Exception as e:
            return None
    
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
        
        # For long emails, split into chunks covering the entire email
        print(f"    [Long email ({len(body)} chars), splitting into chunks...]")
        
        # Split into chunks covering the entire email
        chunks = []
        start = 0
        while start < len(body):
            end = min(start + CHUNK_SIZE, len(body))
            chunk = body[start:end]
            if chunk.strip():
                chunks.append(chunk)
            start = end
        
        if not chunks:
            return "Error: Could not split email into chunks"
        
        print(f"    [Processing {len(chunks)} chunks from beginning...]")
        
        # Summarize each chunk with short summaries (~100 chars)
        chunk_summaries = []
        for i, chunk in enumerate(chunks, 1):
            print(f"    [Chunk {i}/{len(chunks)}: Summarizing ({len(chunk)} chars)...]", end='', flush=True)
            chunk_summary = try_summarize(format_text(chunk, f"Chunk {i}"), max_length=CHUNK_SUMMARY_LENGTH)
            if chunk_summary:
                chunk_summaries.append(chunk_summary)
                print(f" ✓ ({len(chunk_summary)} chars)")
            else:
                print(f" ✗")
        
        if not chunk_summaries:
            return "Error: Could not generate any chunk summaries"
        
        # Combine chunk summaries first
        combined_summaries = " ".join(chunk_summaries)
        print(f"    [Creating coherent summary from {len(chunk_summaries)} chunks...]", end='', flush=True)
        
        # Create a final coherent summary that connects all chunks
        final_text = f"""Email Summary

From: {from_addr}
Subject: {subject}

The email covers the following topics from the beginning:
{combined_summaries}

Please provide a comprehensive, detailed, and coherent summary that is LONG and DETAILED (aim for 600-800 characters). Connect all these topics into a flowing narrative. Include all key points from each section. Make sure the summary is thorough and covers all the important information. Write a longer, more comprehensive summary."""
        
        final_summary = try_summarize(final_text, max_length=None)  # No cap on final summary
        
        # Ensure minimum length of 500 characters
        MIN_SUMMARY_LENGTH = 500
        if final_summary:
            if len(final_summary) < MIN_SUMMARY_LENGTH:
                # If too short, append chunk summaries to reach minimum length
                print(f" ({len(final_summary)} chars, extending to at least {MIN_SUMMARY_LENGTH}...)")
                remaining = MIN_SUMMARY_LENGTH - len(final_summary)
                
                # Add more detail from chunk summaries
                additional = " ".join(chunk_summaries)
                if len(additional) > remaining:
                    # Take enough to reach minimum, but try to end at a sentence
                    additional = additional[:remaining + 100].rsplit('.', 1)[0] + '.'
                
                final_summary = final_summary + " " + additional
                
                # Ensure we're at least at minimum (might be slightly over, that's fine)
                if len(final_summary) < MIN_SUMMARY_LENGTH:
                    # If still too short, add more
                    more_needed = MIN_SUMMARY_LENGTH - len(final_summary)
                    more_text = " ".join(chunk_summaries)
                    if len(more_text) > more_needed:
                        more_text = more_text[:more_needed + 50].rsplit('.', 1)[0] + '.'
                    final_summary = final_summary + " " + more_text
            
            print(f" ✓ ({len(final_summary)} chars)")
            return final_summary
        else:
            # Fallback: use combined summaries directly (should be long enough)
            print(f" ✗ (using combined)")
            return combined_summaries
    
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
    
    # Fetch today's newsletters
    print(f"\nFetching today's newsletters from '{LABEL_NAME}' label...")
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


def create_test_email():
    """Create a test email with content that will require exactly 3 chunks."""
    # CHUNK_SIZE is 1500, so for 3 chunks we need ~4500 chars
    test_content = """The Future of Artificial Intelligence in Healthcare

Artificial intelligence is revolutionizing healthcare in unprecedented ways. Machine learning algorithms can now analyze medical images with greater accuracy than human radiologists in some cases. Deep learning models trained on millions of patient records can predict disease progression and recommend personalized treatment plans. Natural language processing enables AI systems to extract insights from unstructured medical notes and research papers.

The integration of AI into clinical workflows is transforming how doctors diagnose and treat patients. Computer vision systems can detect early signs of cancer in medical scans that might be missed by the human eye. Predictive analytics help hospitals manage resources more efficiently and reduce patient wait times. AI-powered chatbots provide 24/7 patient support and triage services, helping to reduce the burden on healthcare staff.

However, challenges remain in ensuring AI systems are trustworthy and equitable. Bias in training data can lead to disparities in care for different patient populations. Regulatory frameworks are still catching up with the rapid pace of AI development. Healthcare providers must balance the benefits of AI with concerns about patient privacy and data security. The future will require close collaboration between technologists, clinicians, and policymakers to ensure AI serves all patients effectively.

As we look ahead, the potential for AI to improve global health outcomes is immense. From drug discovery to personalized medicine, AI is opening new frontiers in healthcare. The key will be developing these technologies responsibly, with a focus on augmenting human expertise rather than replacing it. The healthcare industry stands at an inflection point, where thoughtful implementation of AI could lead to better outcomes for patients worldwide.""" * 10  # Repeat to get ~7500 chars
    
    return {
        'id': 'test_email_001',
        'subject': 'Test Newsletter: The Future of AI in Healthcare',
        'from': 'test@newsletter.com',
        'date': datetime.now().strftime('%a, %d %b %Y %H:%M:%S %z'),
        'body': test_content[:4500]  # Ensure exactly 3 chunks
    }


if __name__ == '__main__':
    import sys
    
    # Check if running in test mode
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        print("Gmail Newsletter Summarizer - TEST MODE")
        print("=" * 40)
        
        hf_api_key = os.getenv('HF_API_KEY') or os.getenv('HUGGINGFACE_API_KEY')
        if hf_api_key:
            client = InferenceClient(model=HF_MODEL, token=hf_api_key)
        else:
            print("Warning: No HF_API_KEY set. Using public API (may have lower rate limits).")
            client = InferenceClient(model=HF_MODEL)
        
        # Create test email
        test_email = create_test_email()
        print(f"\nTest Email: {test_email['subject']}")
        print(f"From: {test_email['from']}")
        print(f"Body length: {len(test_email['body'])} chars (should create ~3 chunks with CHUNK_SIZE=1500)\n")
        print("=" * 40)
        print("Generating summary...\n")
        
        summary = summarize_email(
            client,
            test_email['subject'],
            test_email['from'],
            test_email['body']
        )
        
        print("\n" + "=" * 40)
        print("FINAL SUMMARY:")
        print("=" * 40)
        print(summary)
        print("=" * 40)
        print(f"\nSummary length: {len(summary)} characters")
    else:
        main()

