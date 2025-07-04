import os
import time
import re
import requests
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, urljoin

import dotenv
from google import genai
from google.genai import types

# Configure Gemini API
client = genai.Client()

# Rate limiting: Gemini free tier allows 15 requests per minute
_last_request_time = 0
_min_request_interval = 7  # 7 seconds between requests (8 requests per minute to be safe)

def _rate_limit():
    """Ensure we don't exceed the Gemini API rate limit"""
    global _last_request_time
    current_time = time.time()
    time_since_last = current_time - _last_request_time
    
    if time_since_last < _min_request_interval:
        wait_time = _min_request_interval - time_since_last
        print(f"Rate limiting: waiting {wait_time:.1f} seconds...")
        time.sleep(wait_time)
    
    _last_request_time = time.time()

def _retry_with_backoff(api_call_func, max_retries: int = 3):
    """
    Execute an API call with retry logic for rate limiting and service errors
    """
    _rate_limit()  # Apply rate limiting before each request
    
    is_content_filtered = False  # Track if error is due to content filtering
    
    for attempt in range(max_retries + 1):
        try:
            return api_call_func()
        except Exception as e:
            error_str = str(e)
            
            # Handle retryable errors:
            # - Rate limiting (429 errors)
            # - Service unavailable (503 errors) 
            # - None response text (only if NOT content filtered)
            # - Other temporary API issues
            # Note: Content filtering blocks are permanent and should not be retried
            is_content_filtered = "CONTENT_FILTERED:" in error_str
            is_retryable = not is_content_filtered and any([
                "429" in error_str,
                "RESOURCE_EXHAUSTED" in error_str,
                "503" in error_str,
                "UNAVAILABLE" in error_str,
                "returned None response text" in error_str,
                "INTERNAL" in error_str,
                "UNKNOWN" in error_str,
                "timeout" in error_str.lower(),
                "connection" in error_str.lower()
            ])
            
            if is_retryable and attempt < max_retries:
                # Determine wait time based on error type
                if "503" in error_str or "UNAVAILABLE" in error_str:
                    # For service unavailable, use shorter initial wait time
                    wait_time = 10 if attempt == 0 else min(30 * (2 ** (attempt - 1)), 120)
                    print(f"Service unavailable (503). Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                elif "returned None response text" in error_str:
                    # For None response text, use moderate wait time
                    wait_time = 15 + (attempt * 10)  # 15s, 25s, 35s
                    print(f"Gemini returned None response (likely content filtering or temporary issue). Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                elif "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    # For rate limiting, use longer wait times
                    wait_time = 60  # Default to 60 seconds for rate limits
                    if "retryDelay" in error_str and "55s" in error_str:
                        wait_time = 55
                    elif attempt > 0:
                        wait_time = min(60 * (2 ** attempt), 300)  # Exponential backoff, max 5 minutes
                    print(f"Rate limit hit. Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                else:
                    # For other retryable errors, use moderate wait time
                    wait_time = 20 + (attempt * 15)  # 20s, 35s, 50s
                    print(f"Temporary API issue: {error_str[:100]}... Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                
                time.sleep(wait_time)
                continue
            elif is_retryable and attempt >= max_retries:
                # Final retry attempt failed
                if "503" in error_str or "UNAVAILABLE" in error_str:
                    raise Exception(f"Service unavailable after {max_retries} retries. "
                                  f"The Gemini API is currently overloaded. Please try again later.")
                elif "returned None response text" in error_str:
                    raise Exception(f"Gemini API returned None response after {max_retries} retries. "
                                  f"This may be due to content filtering or temporary model issues. "
                                  f"Please check your input content and try again later.")
                elif "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    raise Exception(f"Rate limit exceeded after {max_retries} retries. "
                                  f"Gemini API free tier allows 15 requests per minute. "
                                  f"Consider upgrading your plan or waiting before retrying.")
                else:
                    raise Exception(f"Temporary API issue persisted after {max_retries} retries: {error_str}")
            else:
                # Non-retryable error, fail immediately
                if is_content_filtered:
                    raise Exception(f"Gemini API blocked your content due to safety filters. "
                                  f"The prompt contains content that violates Gemini's usage policies. "
                                  f"Please review and modify your input to avoid prohibited content. "
                                  f"Details: {str(e)}")
                else:
                    raise Exception(f"Error making Gemini request: {str(e)}")
    
    # Should never reach here
    raise Exception("Unexpected error in _retry_with_backoff")

def _make_request(prompt, temperature: float = 0.8, max_retries: int = 3):
    """
    Make a request to Gemini API with retry logic for rate limiting
    """
    def api_call():
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=8192,
            )
        )
        
        # Check if response text is None and provide detailed error info
        if response.text is None:
            # Try to get more information about why the response is None
            error_details = []
            is_content_filtered = False
            
            # Check for prompt feedback first (this is where content filtering blocks are reported)
            if hasattr(response, 'prompt_feedback'):
                if hasattr(response.prompt_feedback, 'block_reason'):
                    block_reason = str(response.prompt_feedback.block_reason)
                    error_details.append(f"block_reason: {block_reason}")
                    # Check if this is a content filtering block (permanent, non-retryable)
                    if 'PROHIBITED_CONTENT' in block_reason or 'SAFETY' in block_reason:
                        is_content_filtered = True
                if hasattr(response.prompt_feedback, 'safety_ratings'):
                    error_details.append(f"prompt_safety_ratings: {response.prompt_feedback.safety_ratings}")
            
            # Check if there are any candidates
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'finish_reason'):
                    finish_reason = str(candidate.finish_reason)
                    error_details.append(f"finish_reason: {finish_reason}")
                    # Also check finish reason for safety blocks
                    if 'SAFETY' in finish_reason or 'PROHIBITED' in finish_reason:
                        is_content_filtered = True
                if hasattr(candidate, 'safety_ratings'):
                    error_details.append(f"safety_ratings: {candidate.safety_ratings}")
            
            error_msg = "Gemini API returned None response text"
            if error_details:
                error_msg += f" ({'; '.join(error_details)})"
            
            # If this is a content filtering issue, mark it as non-retryable
            if is_content_filtered:
                error_msg = f"CONTENT_FILTERED: {error_msg}"
            
            print(f"DEBUG: {error_msg}")
            print(f"DEBUG: Full response object: {response}")
            
            raise Exception(error_msg)
        
        return response.text
    
    return _retry_with_backoff(api_call, max_retries)


def get_gemini_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini for text-based prompts
    """
    return _make_request(prompt, temperature)


def gemini_describe_image(image_url: str, temperature: float = 0.01, max_retries: int = 3):
    """
    Describe an image using Gemini's vision capabilities
    """
    try:
        import requests
        from PIL import Image
        import io
        
        # Download the image
        response = requests.get(image_url)
        if response.status_code != 200:
            raise Exception(f"Failed to download image: {response.status_code}")
        
        # Convert to PIL Image
        image = Image.open(io.BytesIO(response.content))
        
        prompt = "What's in this image? Provide a detailed description."
        
        # Define the API call function
        def api_call():
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, image],
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=2048,
                )
            )
            
            # Check if response text is None and provide detailed error info
            if response.text is None:
                # Try to get more information about why the response is None
                error_details = []
                is_content_filtered = False
                
                # Check for prompt feedback first (this is where content filtering blocks are reported)
                if hasattr(response, 'prompt_feedback'):
                    if hasattr(response.prompt_feedback, 'block_reason'):
                        block_reason = str(response.prompt_feedback.block_reason)
                        error_details.append(f"block_reason: {block_reason}")
                        # Check if this is a content filtering block (permanent, non-retryable)
                        if 'PROHIBITED_CONTENT' in block_reason or 'SAFETY' in block_reason:
                            is_content_filtered = True
                    if hasattr(response.prompt_feedback, 'safety_ratings'):
                        error_details.append(f"prompt_safety_ratings: {response.prompt_feedback.safety_ratings}")
                
                # Check if there are any candidates
                if hasattr(response, 'candidates') and response.candidates:
                    candidate = response.candidates[0]
                    if hasattr(candidate, 'finish_reason'):
                        finish_reason = str(candidate.finish_reason)
                        error_details.append(f"finish_reason: {finish_reason}")
                        # Also check finish reason for safety blocks
                        if 'SAFETY' in finish_reason or 'PROHIBITED' in finish_reason:
                            is_content_filtered = True
                    if hasattr(candidate, 'safety_ratings'):
                        error_details.append(f"safety_ratings: {candidate.safety_ratings}")
                
                error_msg = "Gemini API returned None response text for image description"
                if error_details:
                    error_msg += f" ({'; '.join(error_details)})"
                
                # If this is a content filtering issue, mark it as non-retryable
                if is_content_filtered:
                    error_msg = f"CONTENT_FILTERED: {error_msg}"
                
                print(f"DEBUG: {error_msg}")
                print(f"DEBUG: Full response object: {response}")
                
                raise Exception(error_msg)
            
            return response.text
        
        # Use shared retry logic
        return _retry_with_backoff(api_call, max_retries)
        
    except Exception as e:
        raise Exception(f"Error describing image with Gemini: {str(e)}")


def search_web_for_recent_info(query: str, max_results: int = 10) -> str:
    """
    Search the web for recent information using DuckDuckGo search
    Returns formatted search results or error message
    """
    try:
        from duckduckgo_search import DDGS
        
        # Multiple search strategies for comprehensive coverage
        search_queries = [
            f"{query} 2024 OR 2025",  # Recent events
            f"{query} site:gov OR site:edu OR site:org",  # Official sources
            f"{query} news 2024 2025",  # News coverage
            f'"{query}" latest current',  # Exact phrase + recency
        ]
        
        all_results = []
        seen_urls = set()
        max_total_results = max_results * 2  # Get extra for filtering
        
        for search_query in search_queries:
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(search_query, max_results=max_results, safesearch='moderate'))
                    
                    for result in results:
                        title = result.get('title', 'No title')
                        body = result.get('body', 'No description')
                        url = result.get('href', 'No URL')
                        
                        # Skip duplicates and unreliable social media
                        if url in seen_urls:
                            continue
                        if any(domain in url.lower() for domain in ['twitter.com', 'x.com', 'facebook.com', 'instagram.com', 'tiktok.com']):
                            continue
                            
                        seen_urls.add(url)
                        
                        # Prioritize official sources and recent news
                        priority_score = 0
                        if any(domain in url.lower() for domain in ['.gov', '.edu', '.org']):
                            priority_score += 10
                        if any(domain in url.lower() for domain in ['reuters.com', 'ap.org', 'cnn.com', 'nytimes.com', 'washingtonpost.com', 'bbc.com']):
                            priority_score += 8
                        if any(word in title.lower() for word in ['2024', '2025', 'latest', 'breaking', 'just', 'new']):
                            priority_score += 5
                        if any(word in body.lower() for word in ['2024', '2025', 'recent', 'latest', 'today', 'yesterday']):
                            priority_score += 3
                            
                        all_results.append({
                            'title': title,
                            'body': body,
                            'url': url,
                            'query': search_query,
                            'priority': priority_score
                        })
                        
                        if len(all_results) >= max_total_results:
                            break
                    
                    # Check if we have enough results to exit outer loop
                    if len(all_results) >= max_total_results:
                        break
                            
            except Exception as e:
                print(f"Search query '{search_query}' failed: {str(e)}")
                continue
        
        if not all_results:
            return f"No recent web search results found for: {query}"
        
        # Sort by priority score, then take top results
        all_results.sort(key=lambda x: x['priority'], reverse=True)
        top_results = all_results[:max_results]
        
        formatted_results = []
        for i, result in enumerate(top_results):
            formatted_results.append(
                f"Result {i+1} (Priority Score: {result['priority']}):\n"
                f"Title: {result['title']}\n"
                f"Description: {result['body']}\n"
                f"URL: {result['url']}\n"
                f"Search Query: {result['query']}\n"
            )
        
        return f"ENHANCED WEB SEARCH RESULTS for '{query}' (sorted by relevance and recency):\n\n" + "\n".join(formatted_results)
        
    except ImportError:
        return "Web search unavailable (duckduckgo-search package not installed)"
    except Exception as e:
        return f"Web search error: {str(e)}"


def get_gemini_search_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini with enhanced search capabilities.
    Always performs web search to get the most current information available.
    """
    
    # Extract key terms for web search from the post content
    lines = prompt.split('\n')
    post_text = ""
    for line in lines:
        if 'Post text:' in line:
            # Find the post text section
            start_idx = lines.index(line)
            for i in range(start_idx + 1, min(start_idx + 10, len(lines))):
                if lines[i].strip() and not lines[i].startswith('```'):
                    post_text += lines[i] + " "
            break
    
    # Always perform web search for current information
    web_results = ""
    if post_text.strip():
        # Perform web search for recent information
        web_results = search_web_for_recent_info(post_text[:200])  # Limit query length
        
        if "Web search error" in web_results or "unavailable" in web_results:
            print(f"Web search failed: {web_results}")
            web_results = ""
    
    # Get Gemini's response with enhanced prompt including web search results
    enhanced_prompt = prompt
    if web_results:
        enhanced_prompt = f"""{prompt}

--- CURRENT WEB SEARCH RESULTS (USE THESE FOR MOST RECENT INFORMATION) ---
{web_results}

CRITICAL INSTRUCTION: When fact-checking, prioritize information from the WEB SEARCH RESULTS above, as it contains the most current and up-to-date information available. If there's any conflict between your training data and the web search results, defer to the web search results for recent events and current status information.
"""
    
    gemini_response = _make_request(enhanced_prompt, temperature)
    
    return gemini_response


def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract all URLs from text using regex pattern
    """
    # Pattern to match URLs with various protocols and formats
    # Excludes common punctuation that might be at the end of URLs in text
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]()]+(?:\([^\s<>"{}|\\^`\[\]()]*\))?[^\s<>"{}|\\^`\[\]()]*|www\.[^\s<>"{}|\\^`\[\]()]+|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9]*\.[a-zA-Z]{2,}(?:/[^\s<>"{}|\\^`\[\]()]*)?'
    raw_urls = re.findall(url_pattern, text)
    
    # Clean URLs by removing trailing punctuation
    cleaned_urls = []
    for url in raw_urls:
        # Remove trailing punctuation that's commonly at the end of sentences
        cleaned_url = re.sub(r'[)\].,;:!?]+$', '', url.strip())
        if cleaned_url:
            cleaned_urls.append(cleaned_url)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_urls = []
    for url in cleaned_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    
    # Normalize URLs - add https:// if missing
    normalized_urls = []
    for url in unique_urls:
        if not url.startswith(('http://', 'https://')):
            if url.startswith('www.'):
                normalized_urls.append(f'https://{url}')
            else:
                normalized_urls.append(f'https://{url}')
        else:
            normalized_urls.append(url)
    
    return normalized_urls


def fetch_page_content(url: str, timeout: int = 10) -> Tuple[Optional[str], int, str]:
    """
    Fetch page content from URL and return (content, status_code, error_message)
    Returns (None, status_code, error_message) if failed
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
        
        # Check if we got a successful response
        if response.status_code == 200:
            # Try to get text content
            content = response.text
            # Limit content length to avoid overwhelming Gemini
            if len(content) > 50000:  # Limit to ~50KB
                content = content[:50000] + "... [content truncated]"
            return content, response.status_code, ""
        else:
            return None, response.status_code, f"HTTP {response.status_code}"
            
    except requests.exceptions.Timeout:
        return None, 0, "Request timeout"
    except requests.exceptions.ConnectionError:
        return None, 0, "Connection error"
    except requests.exceptions.TooManyRedirects:
        return None, 0, "Too many redirects"
    except Exception as e:
        return None, 0, f"Error: {str(e)}"


def _needs_current_verification(text: str) -> bool:
    """
    Determine if the content likely contains claims that need current verification
    """
    current_keywords = [
        # Time indicators
        '2024', '2025', 'recent', 'latest', 'just', 'new', 'current', 'now', 'today', 'yesterday',
        'this year', 'last year', 'recently', 'breaking', 'announced', 'declared', 'signed',
        
        # Political/election keywords
        'mayor', 'election', 'primary', 'candidate', 'running for', 'campaign', 'elected',
        'won', 'victory', 'defeated', 'conceded', 'nominee', 'race', 'vote', 'ballot',
        
        # Government/policy keywords
        'bill', 'law', 'policy', 'administration', 'congress', 'senate', 'house',
        'governor', 'president', 'passed', 'legislation', 'executive order',
        
        # Status change keywords
        'is now', 'has become', 'appointed', 'resigned', 'stepped down', 'takes office',
        'announced', 'confirmed', 'approved', 'rejected', 'withdrew', 'endorsed'
    ]
    
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in current_keywords)

def validate_page_content_with_gemini(url: str, content: str, original_claim: str) -> Tuple[bool, str]:
    """
    Use Gemini to validate if page content is relevant and not a 404/error page
    Returns (is_valid, explanation)
    """
    
    # Check if this is a Twitter/X URL that might be deleted/eliminated
    parsed_url = urlparse(url.lower())
    is_twitter_url = parsed_url.netloc in ['twitter.com', 'x.com', 'www.twitter.com', 'www.x.com', 'mobile.twitter.com', 'm.twitter.com']
    
    # Check for common indicators of deleted/eliminated Twitter posts
    if is_twitter_url:
        deleted_indicators = [
            "this post is from a suspended account",
            "this post has been deleted",
            "this tweet is unavailable", 
            "this account owner limits who can view",
            "tweet not found",
            "post not found",
            "account suspended",
            "page doesn't exist",
            "something went wrong",
            "try again",
            "hmm...this page doesn't exist",
            "sorry, you are not authorized to see this status",
            "this tweet was deleted"
        ]
        
        content_lower = content.lower()
        for indicator in deleted_indicators:
            if indicator in content_lower:
                return False, f"Eliminated/deleted Twitter/X post: {indicator}"
    
    # Enhanced validation for current events
    needs_current_info = _needs_current_verification(original_claim)
    
    prompt = f"""You are validating whether a web page is useful as a source for fact-checking.

Original claim/context: {original_claim[:500]}...

URL: {url}

Page content (first part):
{content[:3000]}...

IMPORTANT: Pay special attention to the current date context. Today is 2025, so be very careful about claims involving recent events from late 2024 through 2025.

{"EXTRA SCRUTINY REQUIRED: The original claim appears to involve recent events or current status that may have changed. This page MUST contain current, up-to-date information to be valid." if needs_current_info else ""}

Please analyze this page and respond with exactly one of these formats:

VALID: [brief explanation of why this page is a good source]
INVALID: [brief explanation of why this page is not useful - e.g., 404 error, irrelevant content, broken page, deleted social media post, etc.]

The page should be considered INVALID if:
- It's a 404 or error page
- It's completely irrelevant to the original claim
- It's a generic homepage without specific information
- It contains mostly ads or navigation without substantive content
- It's broken or corrupted content
- It's a deleted/eliminated social media post (Twitter/X)
- For Twitter/X URLs: shows "Tweet not found", "Account suspended", "This tweet was deleted", or similar messages
- The information is clearly outdated and contradicts more recent events (especially for 2024-2025 events)
{"- The page contains outdated information about recent events when current information is critically needed" if needs_current_info else ""}

The page should be considered VALID if:
- It contains relevant factual information related to the claim
- It's from a recognizable news source, government site, or credible organization
- It has substantive content that could be used for fact-checking
- For recent events (2024-2025): the source has current, up-to-date information
{"- The information is demonstrably current and addresses recent developments mentioned in the claim" if needs_current_info else ""}
"""

    try:
        response = get_gemini_response(prompt, temperature=0.3)
        if response is None:
            return False, "Failed to get validation response from Gemini"
        
        response = response.strip()
        if response.startswith("VALID:"):
            return True, response[6:].strip()
        elif response.startswith("INVALID:"):
            return False, response[8:].strip()
        else:
            # If format is unexpected, err on the side of caution
            return False, f"Unexpected validation response format: {response[:100]}"
            
    except Exception as e:
        return False, f"Error validating with Gemini: {str(e)}"


def verify_and_filter_links(search_results: str, original_query: str) -> Tuple[Optional[str], List[str]]:
    """
    Extract URLs from search results, verify they're valid and relevant, 
    and return filtered search results with only valid links
    
    Returns (filtered_search_results, valid_urls)
    """
    print("🔍 Extracting and verifying links from search results...")
    
    # Extract all URLs from the search results
    urls = extract_urls_from_text(search_results)
    
    if not urls:
        print("  ❌ No URLs found in search results")
        return search_results, []
    
    print(f"  📋 Found {len(urls)} URLs to verify")
    
    valid_urls = []
    url_validation_results = {}
    
    # Verify each URL
    for i, url in enumerate(urls):
        print(f"  🔗 Checking URL {i+1}/{len(urls)}: {url}")
        
        # Fetch page content
        content, status_code, error_msg = fetch_page_content(url)
        
        if content is None:
            # If we can't fetch the content (404, 403, timeout, etc.), mark as invalid immediately
            print(f"    ❌ Failed to fetch: {error_msg}")
            url_validation_results[url] = (False, f"Failed to fetch: {error_msg}")
            continue
        
        # Only validate with Gemini if we successfully fetched content
        is_valid, explanation = validate_page_content_with_gemini(url, content, original_query)
        
        if is_valid:
            print(f"    ✅ Valid: {explanation}")
            valid_urls.append(url)
            url_validation_results[url] = (True, explanation)
        else:
            print(f"    ❌ Invalid: {explanation}")
            url_validation_results[url] = (False, explanation)
    
    print(f"📊 Link verification complete: {len(valid_urls)}/{len(urls)} URLs are valid")
    
    # Filter the search results to only include valid URLs
    if len(valid_urls) == 0:
        print("❌ No valid sources found - canceling note generation")
        return None, []  # Return None to indicate no valid sources
    
    print(f"✅ Found {len(valid_urls)} valid sources - proceeding with note generation")
    
    # Create a filtered version that emphasizes only valid sources
    filtered_results = f"""VERIFIED VALID SOURCES (ONLY USE THESE):
{chr(10).join(f"✅ {url}" for url in valid_urls)}

ORIGINAL SEARCH RESULTS:
{search_results}

IMPORTANT: Only use information that can be attributed to the VERIFIED VALID SOURCES listed above. 
Do not use any information from sources marked as invalid, broken, or 404.
If you reference information in your note, only cite URLs from the VERIFIED VALID SOURCES list."""
    
    return filtered_results, valid_urls


if __name__ == "__main__":
    dotenv.load_dotenv()
    print(
        get_gemini_search_response(
            "Provide me a digest of world news in the last 2 hours. Please respond with links to each source next to the claims that the source supports."
        )
    )
