import os
import time
import re
import requests
import random
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import urlparse, urljoin

import dotenv
from google import genai
from google.genai import types

# Configure Gemini API
client = genai.Client()

# Rate limiting: Gemini free tier allows 15 requests per minute
_last_request_time = 0
_min_request_interval = 7  # 7 seconds between requests (8 requests per minute to be safe)

# Simple cache for search results to avoid duplicate API calls
_search_cache = {}
_cache_expiry_seconds = 300  # 5 minutes

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
    Search the web for recent information using multiple search engines with fallback
    Returns formatted search results or error message
    """
    # Check cache first to avoid duplicate API calls
    cache_key = f"{query.strip()[:100]}_{max_results}"  # Limit key length
    current_time = time.time()
    
    # Clean expired cache entries
    expired_keys = [k for k, (timestamp, _) in _search_cache.items() 
                   if current_time - timestamp > _cache_expiry_seconds]
    for k in expired_keys:
        del _search_cache[k]
    
    # Return cached result if available and not expired
    if cache_key in _search_cache:
        timestamp, cached_result = _search_cache[cache_key]
        if current_time - timestamp <= _cache_expiry_seconds:
            print(f"📋 Using cached search results for: {query[:50]}...")
            return cached_result
    
    print(f"🔍 Starting multi-engine search for: {query[:50]}...")
    
    # Try multiple search engines in order of preference
    search_engines = [
        ("Google (yagooglesearch)", _search_with_yagooglesearch), 
        ("RSS Feeds", _search_with_rss_feeds),
        ("News Scraper", _search_with_news_scraper)
    ]
    
    for engine_name, search_func in search_engines:
        try:
            print(f"🔍 Trying {engine_name}...")
            results = search_func(query, max_results)
            
            # Check if the result is a failure message from any search engine
            failure_patterns = [
                "Web search error",  # General error
                "Google search rate limited or no results found",  # Google
                "No valid results found from Google search",  # Google
                "No relevant news found in RSS feeds",  # RSS
                "No articles found through web scraping",  # News Scraper
            ]
            
            is_failure = not results or any(pattern in results for pattern in failure_patterns)
            
            if not is_failure:
                # Extract result count from the results string
                result_count = 0
                if "Result 1" in results:
                    # Count the number of "Result X:" or "Result X (Priority:" patterns
                    result_count = len(re.findall(r'Result \d+(?:\s*\(Priority:\s*\d+\))?:', results))
                
                print(f"✅ {engine_name} successful - found {result_count} results")
                # Cache the successful result
                _search_cache[cache_key] = (current_time, results)
                return results
            else:
                print(f"⚠️ {engine_name} returned no results")
                
        except Exception as e:
            print(f"❌ {engine_name} failed: {str(e)}")
            continue
    
    # If all engines fail, return a helpful error message
    error_msg = f"❌ All search engines failed for query: {query}. Please try again later or check your internet connection."
    _search_cache[cache_key] = (current_time, error_msg)
    return error_msg



def _search_with_yagooglesearch(query: str, max_results: int = 10) -> str:
    """
    Google search using yagooglesearch library with rate limit handling
    """
    try:
        # Try importing with proper error handling
        try:
            import yagooglesearch  # type: ignore
        except ImportError:
            print("    ❌ yagooglesearch package not installed. Install with: pip install yagooglesearch")
            return "Google search rate limited or no results found"
        
        # Clean query for Google search and try multiple query strategies
        clean_query = query.strip()[:200]  # Increased limit
        
        # Try different search strategies, prioritizing recent information
        search_strategies = [
            f"{clean_query} 2025",  # Current year first
            f"{clean_query} 2024",  # Previous year 
            f"{clean_query} news 2024 OR 2025",  # News with recent years
            clean_query,  # Original query
            f'"{clean_query}"',  # Exact phrase
        ]
        
        for strategy_idx, search_query in enumerate(search_strategies):
            try:
                print(f"  🔍 Google strategy {strategy_idx + 1}: {search_query[:80]}...")
                
                client = yagooglesearch.SearchClient(
                    search_query,
                    tbs="li:1",  # Verbatim search
                    max_search_result_urls_to_return=max_results * 3,  # Get more results to filter
                    http_429_cool_off_time_in_minutes=3,  # Longer cooloff
                    http_429_cool_off_factor=2.0,  # Increased factor
                    minimum_delay_between_paged_results_in_seconds=3,  # Longer delay
                    verbosity=1,  # Minimal verbosity
                    verbose_output=False,
                    yagooglesearch_manages_http_429s=True
                )
                
                client.assign_random_user_agent()
                urls = client.search()
                
                if urls and "HTTP_429_DETECTED" not in urls:
                    # Process the URLs to get titles and descriptions
                    results = []
                    seen_urls = set()
                    
                    for url in urls[:max_results * 2]:  # Process more URLs
                        if url in seen_urls or _should_skip_url(url):
                            continue
                        
                        seen_urls.add(url)
                        
                        # Try to get page title and description with better error handling
                        try:
                            response = requests.get(url, timeout=8, headers={
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                            })
                            
                            if response.status_code == 200:
                                from bs4 import BeautifulSoup
                                
                                # Handle encoding properly
                                try:
                                    # Try to get encoding from response headers
                                    content = response.content
                                    if response.encoding:
                                        content = response.content.decode(response.encoding, errors='replace')
                                    else:
                                        content = response.content.decode('utf-8', errors='replace')
                                    
                                    soup = BeautifulSoup(content, 'html.parser')
                                except UnicodeDecodeError:
                                    # Fallback to replacing problematic characters
                                    soup = BeautifulSoup(response.content, 'html.parser', from_encoding='utf-8')
                                
                                title = "No title"
                                if soup.title and soup.title.string:
                                    title = soup.title.string.strip()[:200]  # Limit title length
                                
                                description = "No description"
                                # Try multiple meta description selectors
                                meta_desc = soup.find('meta', attrs={'name': 'description'}) or \
                                           soup.find('meta', attrs={'property': 'og:description'}) or \
                                           soup.find('meta', attrs={'name': 'twitter:description'})
                                
                                if meta_desc and hasattr(meta_desc, 'get'):
                                    content = meta_desc.get('content')  # type: ignore
                                    if isinstance(content, str):
                                        description = content.strip()[:300]  # Limit description length
                                
                                # If no meta description, try to get text from the page
                                if description == "No description":
                                    paragraphs = soup.find_all('p')
                                    if paragraphs:
                                        description = ' '.join([p.get_text().strip() for p in paragraphs[:3]])[:300]
                                
                                results.append({
                                    'title': title,
                                    'description': description,
                                    'url': url,
                                    'priority': _calculate_priority_score(title, description, url, query)
                                })
                                
                        except Exception as e:
                            # If we can't get details, still include the URL with basic info
                            results.append({
                                'title': url.split('/')[-1] if '/' in url else url,
                                'description': f"Description unavailable: {str(e)[:100]}",
                                'url': url,
                                'priority': 1
                            })
                        
                        if len(results) >= max_results:
                            break
                    
                    if results:
                        # Sort by priority and format results
                        results.sort(key=lambda x: x['priority'], reverse=True)
                        
                        formatted_results = []
                        for i, result in enumerate(results[:max_results]):
                            formatted_results.append(
                                f"Result {i+1} (Priority: {result['priority']}):\n"
                                f"Title: {result['title']}\n"
                                f"Description: {result['description']}\n"
                                f"URL: {result['url']}\n"
                            )
                        
                        return f"RECENT WEB SEARCH RESULTS for '{query}' (Google):\n\n" + "\n".join(formatted_results)
                else:
                    print(f"    ⚠️ Strategy {strategy_idx + 1} failed: rate limited or no results")
                    
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate limit" in error_str:
                    print(f"    🚫 Google rate limit hit on strategy {strategy_idx + 1}")
                    if strategy_idx < len(search_strategies) - 1:
                        print(f"    ⏳ Waiting before trying next strategy...")
                        time.sleep(5)  # Wait between strategies
                        continue
                else:
                    print(f"    ❌ Google search error on strategy {strategy_idx + 1}: {str(e)}")
                    continue
        
        return "Google search rate limited or no results found"
        
    except Exception as e:
        raise Exception(f"Google search error: {str(e)}")


def _search_with_rss_feeds(query: str, max_results: int = 10) -> str:
    """
    Search recent news using RSS feeds from major news sources
    """
    try:
        try:
            import feedparser  # type: ignore
        except ImportError:
            raise Exception("feedparser package not installed. Install with: pip install feedparser")
        
        # Expanded list of major news RSS feeds
        rss_feeds = [
            "https://rss.cnn.com/rss/edition.rss",
            "https://feeds.bbci.co.uk/news/rss.xml",
            "https://www.reuters.com/rssFeed/worldNews",
            "https://rss.ap.org/rss/apf-topnews.rss",
            "https://feeds.npr.org/1001/rss.xml",
            "https://abcnews.go.com/abcnews/topstories",
            "https://feeds.nbcnews.com/nbcnews/public/news",
            "https://feeds.foxnews.com/foxnews/latest",
            "https://feeds.washingtonpost.com/rss/world",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://feeds.bloomberg.com/markets/news.rss",
            "https://feeds.theguardian.com/theguardian/world/rss",
            "https://feeds.politico.com/politico/rss",
            "https://feeds.huffingtonpost.com/huffingtonpost/raw_feed",
            "https://feeds.usatoday.com/usatoday-NewsTopStories"
        ]
        
        all_entries = []
        query_lower = query.lower()
        query_terms = query_lower.split()
        
        # Create more sophisticated search terms
        search_terms = query_terms.copy()
        
        # Add key terms from the query for better matching
        import re
        capitalized_terms = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', query)
        for term in capitalized_terms:
            search_terms.append(term.lower())
        
        # Extract numbers and years for better matching
        numbers = re.findall(r'\b\d+\b', query)
        search_terms.extend(numbers)
        
        print(f"🔍 Searching RSS feeds with terms: {search_terms[:10]}...")
        
        successful_feeds = 0
        failed_feeds = 0
        
        for feed_url in rss_feeds:
            try:
                print(f"  📡 Checking feed: {feed_url}")
                feed = feedparser.parse(feed_url)
                
                # Check if the feed was parsed successfully
                if hasattr(feed, 'status') and feed.status >= 400:
                    print(f"    ❌ Feed returned status {feed.status}")
                    failed_feeds += 1
                    continue
                
                if not hasattr(feed, 'entries') or not feed.entries:
                    print(f"    ❌ No entries found in feed")
                    failed_feeds += 1
                    continue
                
                feed_entries_found = 0
                for entry in feed.entries[:50]:  # Check more entries per feed
                    title = entry.get('title', '')
                    summary = entry.get('summary', entry.get('description', ''))
                    link = entry.get('link', '')
                    
                    # More sophisticated relevance checking
                    content_to_check = (title + ' ' + summary).lower()
                    
                    # Check if any search terms appear in the content
                    relevance_score = 0
                    for term in search_terms:
                        if term in content_to_check:
                            relevance_score += 3
                        # Check for partial matches
                        if any(term in word for word in content_to_check.split()):
                            relevance_score += 1
                    
                    if relevance_score > 0:
                        # Safely extract domain from URL
                        try:
                            from urllib.parse import urlparse
                            parsed_url = urlparse(feed_url)
                            domain = parsed_url.netloc or feed_url
                        except Exception:
                            domain = feed_url
                        
                        all_entries.append({
                            'title': title,
                            'summary': summary,
                            'link': link,
                            'source': domain,
                            'relevance': relevance_score
                        })
                        feed_entries_found += 1
                
                if feed_entries_found > 0:
                    print(f"    ✅ Found {feed_entries_found} relevant entries")
                    successful_feeds += 1
                else:
                    print(f"    ⚠️ No relevant entries found")
                    
            except Exception as e:
                print(f"    ❌ Failed to parse RSS feed {feed_url}: {e}")
                failed_feeds += 1
                continue
        
        print(f"📊 RSS Search Summary: {successful_feeds} successful feeds, {failed_feeds} failed feeds")
        
        if not all_entries:
            return "No relevant news found in RSS feeds"
        
        # Sort by relevance and take top results
        all_entries.sort(key=lambda x: x['relevance'], reverse=True)
        top_entries = all_entries[:max_results]
        
        # Format results
        formatted_results = []
        for i, entry in enumerate(top_entries):
            formatted_results.append(
                f"Result {i+1} (Relevance: {entry['relevance']}):\n"
                f"Title: {entry['title']}\n"
                f"Description: {entry['summary'][:300]}...\n"
                f"URL: {entry['link']}\n"
                f"Source: {entry['source']}\n"
            )
        
        return f"RECENT NEWS from RSS FEEDS for '{query}':\n\n" + "\n".join(formatted_results)
        
    except Exception as e:
        raise Exception(f"RSS feed search error: {str(e)}")


def _search_with_news_scraper(query: str, max_results: int = 10) -> str:
    """
    Scrape news from aggregator sites
    """
    try:
        from bs4 import BeautifulSoup
        
        # Try scraping from news aggregator sites
        aggregators = [
            "https://news.google.com/search?q=" + query.replace(' ', '%20'),
            "https://www.allsides.com/search?search=" + query.replace(' ', '+')
        ]
        
        all_articles = []
        
        for url in aggregators:
            try:
                response = requests.get(url, timeout=10, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, 'html.parser')
                    
                    # Look for article-like elements
                    articles = soup.find_all(['article', 'div'], class_=re.compile(r'(article|news|story|item)', re.I))
                    
                    for article in articles[:max_results]:
                        title_elem = article.find(['h1', 'h2', 'h3', 'h4'], text=True)
                        link_elem = article.find('a', href=True)
                        
                        if title_elem and link_elem:
                            title = title_elem.get_text().strip()
                            link = link_elem.get('href')
                            
                            # Make relative URLs absolute
                            if link.startswith('/'):
                                from urllib.parse import urljoin
                                link = urljoin(url, link)
                            
                            # Safely extract domain from URL
                            try:
                                from urllib.parse import urlparse
                                parsed_url = urlparse(url)
                                domain = parsed_url.netloc or url
                            except Exception:
                                domain = url
                                
                            all_articles.append({
                                'title': title,
                                'url': link,
                                'source': domain
                            })
                            
            except Exception as e:
                print(f"Failed to scrape {url}: {e}")
                continue
        
        if not all_articles:
            return "No articles found through web scraping"
        
        # Remove duplicates and format results
        seen_urls = set()
        unique_articles = []
        
        for article in all_articles:
            if article['url'] not in seen_urls:
                seen_urls.add(article['url'])
                unique_articles.append(article)
                
        unique_articles = unique_articles[:max_results]
        
        formatted_results = []
        for i, article in enumerate(unique_articles):
            formatted_results.append(
                f"Result {i+1}:\n"
                f"Title: {article['title']}\n"
                f"URL: {article['url']}\n"
                f"Source: {article['source']}\n"
            )
        
        return f"RECENT NEWS from WEB SCRAPING for '{query}':\n\n" + "\n".join(formatted_results)
        
    except ImportError:
        raise Exception("BeautifulSoup package not installed")
    except Exception as e:
        raise Exception(f"News scraping error: {str(e)}")




def _calculate_relevance_score(text: str, query: str) -> int:
    """
    Calculate relevance score for RSS feed entries
    """
    score = 0
    text_lower = text.lower()
    query_terms = query.lower().split()
    
    for term in query_terms:
        # Exact term matches
        score += text_lower.count(term) * 3
        
        # Partial matches
        for word in text_lower.split():
            if term in word:
                score += 1
    
    return score


def _build_comprehensive_search_query(original_query: str) -> str:
    """
    Build a single comprehensive search query instead of multiple separate queries
    This reduces API calls and improves efficiency
    """
    import re
    
    # Increased query length limit to allow for more detailed searches
    query = original_query.strip()[:300]  # Increased from 150 to 300 characters
    
    # Remove problematic characters that might cause search issues
    query = query.replace('```', '').replace('"', '').strip()
    
    # If query is too short, return it as-is to avoid over-complicating simple queries
    if len(query) < 10:
        return query
    
    # Extract important elements from the query to build a more targeted search
    capitalized_terms = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', query)
    years = re.findall(r'\b(20[0-9]{2})\b', query)
    numbers = re.findall(r'\b\d+\b', query)
    
    # Remove years from numbers to avoid duplication
    non_year_numbers = [num for num in numbers if num not in years]
    
    # Build a comprehensive query that covers what the multiple queries were trying to achieve
    # Instead of 4 separate API calls, use search operators in a single call
    query_parts = [f'"{query}"']
    
    # Add capitalized terms (likely proper nouns, names, places) for more specific searches
    if capitalized_terms:
        for term in capitalized_terms[:3]:  # Limit to avoid overly long queries
            query_parts.append(f'"{term}"')
    
    # Add year-specific searches if years are found (but only if not already in query)
    if years:
        for year in years[:2]:  # Limit to avoid overly long queries
            # Only add if this year isn't already mentioned in the original query
            if year not in original_query:
                query_parts.append(f'({query} {year})')
    
    # Add recency indicators (2024/2025) unless they're already present in the query
    if '2024' not in original_query:
        query_parts.append(f'({query} 2024)')
    if '2025' not in original_query:
        query_parts.append(f'({query} 2025)')
    
    # Add non-year numbers for more specific searches (could be important figures, statistics, etc.)
    if non_year_numbers:
        for num in non_year_numbers[:2]:  # Limit to avoid overly long queries
            # Only add if this number isn't already mentioned in the original query
            if num not in original_query:
                query_parts.append(f'("{query} {num}")')
    
    # Add context-specific searches
    query_parts.append(f'({query} news)')
    query_parts.append(f'({query} official)')
    
    # Join all parts with OR to create comprehensive search
    enhanced_query = ' OR '.join(query_parts)
    
    return enhanced_query


def _should_skip_url(url: str) -> bool:
    """
    Determine if a URL should be skipped based on domain/quality
    """
    url_lower = url.lower()
    
    # Skip social media and low-quality sources
    skip_domains = [
        'twitter.com', 'x.com', 'facebook.com', 'instagram.com', 'tiktok.com',
        'pinterest.com', 'reddit.com', 'youtube.com', 'youtu.be',
        'blogspot.com', 'wordpress.com', 'medium.com/@'  # Skip personal blogs
    ]
    
    return any(domain in url_lower for domain in skip_domains)


def _calculate_priority_score(title: str, body: str, url: str, original_query: str) -> int:
    """
    Calculate a priority score for search results based on multiple factors
    """
    score = 0
    title_lower = title.lower()
    body_lower = body.lower()
    url_lower = url.lower()
    query_lower = original_query.lower()
    
    # Official and credible sources get high priority
    if any(domain in url_lower for domain in ['.gov', '.edu', '.org']):
        score += 15
    
    # Major news sources get high priority
    news_domains = [
        'reuters.com', 'ap.org', 'cnn.com', 'nytimes.com', 'washingtonpost.com', 
        'bbc.com', 'npr.org', 'wsj.com', 'guardian.com', 'bloomberg.com'
    ]
    if any(domain in url_lower for domain in news_domains):
        score += 12
    
    # Other news sources get medium priority
    news_indicators = ['news', 'press', 'times', 'post', 'journal', 'herald']
    if any(indicator in url_lower for indicator in news_indicators):
        score += 8
    
    # Recency indicators in title
    recent_words = ['2024', '2025', 'latest', 'breaking', 'just', 'new', 'recent', 'today']
    score += sum(3 for word in recent_words if word in title_lower)
    
    # Recency indicators in description
    score += sum(2 for word in recent_words if word in body_lower)
    
    # Query relevance - exact phrase matches
    if query_lower[:50] in title_lower:  # Limit query length for comparison
        score += 10
    if query_lower[:50] in body_lower:
        score += 5
    
    # Quality indicators
    quality_words = ['official', 'announcement', 'confirmed', 'verified', 'statement']
    score += sum(2 for word in quality_words if word in title_lower or word in body_lower)
    
    return score

def get_gemini_search_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini with enhanced search capabilities.
    Always performs web search to get the most current information available.
    """
    
    # Extract key terms for web search from the post content with improved logic
    lines = prompt.split('\n')
    post_text = ""
    
    # Look for the post text section with multiple possible formats
    post_text_indicators = ['Post text:', 'post text:', 'POST TEXT:']
    
    for line in lines:
        if any(indicator in line for indicator in post_text_indicators):
            # Find the post text section
            start_idx = lines.index(line)
            
            # Extract text from multiple lines after the indicator
            for i in range(start_idx + 1, min(start_idx + 20, len(lines))):
                if i < len(lines):
                    current_line = lines[i].strip()
                    
                    # Stop if we hit another section or formatting
                    if current_line.startswith('```') or current_line.startswith('*') or current_line.startswith('Summary of images'):
                        break
                    
                    # Add non-empty lines to post text
                    if current_line:
                        post_text += current_line + " "
            break
    
    # If no explicit post text found, try to extract from the entire prompt
    if not post_text.strip():
        # Look for quoted content or text that looks like a post
        import re
        
        # Try to find text within quotes or after certain patterns
        quote_matches = re.findall(r'```\s*([^`]+)\s*```', prompt, re.DOTALL)
        if quote_matches:
            post_text = quote_matches[0].strip()
        else:
            # Extract text that looks like social media content (has certain characteristics)
            lines_without_formatting = [line.strip() for line in lines if line.strip() and not line.startswith(('*', '```', 'You will', 'Instructions:', 'CURRENT DATE', 'CRITICAL:', 'IMPORTANT:', 'SPECIAL'))]
            if lines_without_formatting:
                post_text = " ".join(lines_without_formatting[:10])  # Take first 10 relevant lines
    
    # Clean and prepare the search query
    if post_text.strip():
        # Remove URLs and handles from the post text for better searching
        post_text = re.sub(r'https?://\S+', '', post_text)
        post_text = re.sub(r'@\w+', '', post_text)
        post_text = re.sub(r'#\w+', '', post_text)
        post_text = post_text.strip()
    
    # Always perform web search for current information
    web_results = ""
    if post_text.strip():
        # Increased search query length to allow for more detailed searches
        search_query = post_text[:1200]  # Increased from 800 to 1200 characters
        print(f"🔍 Searching with query: {search_query[:100]}...")
        
        # Perform web search for recent information using the enhanced query
        web_results = search_web_for_recent_info(search_query)
        
        if "Web search error" in web_results or "unavailable" in web_results:
            print(f"Web search failed: {web_results}")
            web_results = ""
    else:
        print("⚠️ No post text found to search with")
    
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
        
        # Check if we got a successful response
        if response.status_code == 200:
            # Try to get text content with proper encoding handling
            try:
                # Try to use the response's encoding if available
                if response.encoding:
                    content = response.content.decode(response.encoding, errors='replace')
                else:
                    # Try UTF-8 as fallback
                    content = response.content.decode('utf-8', errors='replace')
                    
                # Limit content length to avoid overwhelming Gemini
                if len(content) > 50000:  # Limit to ~50KB
                    content = content[:50000] + "... [content truncated]"
                return content, response.status_code, ""
            except UnicodeDecodeError:
                # If decoding fails, use response.text as fallback
                content = response.text
                if len(content) > 50000:
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

The page should be considered INVALID ONLY if:
- It's clearly a 404 or error page
- It's completely irrelevant to the original claim (no connection at all)
- It's a deleted/eliminated social media post (Twitter/X)
- For Twitter/X URLs: shows "Tweet not found", "Account suspended", "This tweet was deleted", or similar messages
- It contains only ads or navigation without ANY substantive content
- It's clearly broken or corrupted content

The page should be considered VALID if:
- It contains ANY relevant factual information related to the claim (even if not perfect)
- It's from a recognizable news source, government site, or credible organization
- It has substantive content that could be used for fact-checking
- For recent events (2024-2025): the source has information that could be relevant to current events
- It's a legitimate website that loaded successfully and contains real content
{"- The information could be useful for understanding recent developments mentioned in the claim" if needs_current_info else ""}

BE GENEROUS in validation - if there's ANY doubt about whether the page could be useful, mark it as VALID. We want to include sources that could potentially help with fact-checking rather than being overly restrictive.
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
    
    # Verify each URL with more lenient validation
    for i, url in enumerate(urls):
        print(f"  🔗 Checking URL {i+1}/{len(urls)}: {url}")
        
        # Check if we should skip this URL based on domain
        if _should_skip_url(url):
            print(f"    ❌ Skipping low-quality domain: {url}")
            url_validation_results[url] = (False, "Low-quality domain")
            continue
        
        # Add rate limiting between requests
        if i > 0:
            print(f"Rate limiting: waiting 2.4 seconds...")
            time.sleep(2.4)
        
        # Fetch page content
        content, status_code, error_msg = fetch_page_content(url)
        
        if content is None:
            # For major errors like 404, 403, mark as invalid
            if status_code in [404, 403]:
                print(f"    ❌ Failed to fetch: {error_msg}")
                url_validation_results[url] = (False, f"Failed to fetch: {error_msg}")
                continue
            else:
                # For other errors (timeout, connection issues), still mark as invalid but less strict
                print(f"    ⚠️ Fetch issues but trying to include: {error_msg}")
                # Don't continue, let it be validated by Gemini with empty content
                content = f"Unable to fetch content: {error_msg}"
        
        # Only validate with Gemini if we successfully fetched content OR if it's a fetch error that might be temporary
        is_valid, explanation = validate_page_content_with_gemini(url, content or "", original_query)
        
        if is_valid:
            print(f"    ✅ Valid: {explanation}")
            valid_urls.append(url)
            url_validation_results[url] = (True, explanation)
        else:
            print(f"    ❌ Invalid: {explanation}")
            url_validation_results[url] = (False, explanation)
    
    print(f"📊 Link verification complete: {len(valid_urls)}/{len(urls)} URLs are valid")
    
    # Show detailed validation results for debugging
    print("📋 Detailed validation results:")
    for url, (is_valid, explanation) in url_validation_results.items():
        status_icon = "✅" if is_valid else "❌"
        print(f"  {status_icon} {url}: {explanation}")
    
    # Filter the search results to only include valid URLs
    if len(valid_urls) == 0:
        print("❌ No valid sources found - canceling note generation")
        print("🔍 This might indicate overly strict validation or network issues")
        return None, []  # Return None to indicate no valid sources
    
    print(f"✅ Found {len(valid_urls)} valid sources - proceeding with note generation")
    print(f"🎯 Valid sources: {', '.join(valid_urls)}")
    
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
