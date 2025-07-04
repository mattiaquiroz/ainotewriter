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
        ("DuckDuckGo", _search_with_duckduckgo),
        ("Google (yagooglesearch)", _search_with_yagooglesearch), 
        ("RSS Feeds", _search_with_rss_feeds),
        ("News Scraper", _search_with_news_scraper),
        ("Alternative DDG", _search_with_alternative_ddg)
    ]
    
    for engine_name, search_func in search_engines:
        try:
            print(f"🔍 Trying {engine_name}...")
            results = search_func(query, max_results)
            
            # Check if the result is a failure message from any search engine
            failure_patterns = [
                "No recent web search results found",  # DuckDuckGo
                "Web search error",  # General error
                "Google search rate limited or no results found",  # Google
                "No valid results found from Google search",  # Google
                "No relevant news found in RSS feeds",  # RSS
                "No articles found through web scraping",  # News Scraper
                "No results found with alternative DuckDuckGo",  # Alternative DDG
            ]
            
            is_failure = not results or any(pattern in results for pattern in failure_patterns)
            
            if not is_failure:
                print(f"✅ {engine_name} successful - found results")
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


def _search_with_duckduckgo(query: str, max_results: int = 10) -> str:
    """
    Original DuckDuckGo search implementation with enhanced error handling
    """
    try:
        from duckduckgo_search import DDGS
        
        # Create a single comprehensive search query instead of multiple separate searches
        enhanced_query = _build_comprehensive_search_query(query)
        
        all_results = []
        seen_urls = set()
        max_attempts = 2  # Reduced attempts for faster fallback
        base_delay = 1.0  # Reduced delay for faster fallback
        
        for attempt in range(max_attempts):
            try:
                if attempt > 0:
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    time.sleep(delay)
                
                with DDGS() as ddgs:
                    search_results = list(ddgs.text(
                        enhanced_query, 
                        max_results=max_results * 2,
                        safesearch='moderate',
                        region='wt-wt',
                        timelimit='m'
                    ))
                    
                    for result in search_results:
                        title = result.get('title', 'No title')
                        body = result.get('body', 'No description')
                        url = result.get('href', 'No URL')
                        
                        if url in seen_urls or _should_skip_url(url):
                            continue
                            
                        seen_urls.add(url)
                        priority_score = _calculate_priority_score(title, body, url, query)
                        
                        all_results.append({
                            'title': title,
                            'body': body,
                            'url': url,
                            'priority': priority_score
                        })
                        
                        if len(all_results) >= max_results:
                            break
                
                if all_results:
                    break
                    
            except Exception as e:
                error_str = str(e).lower()
                if any(indicator in error_str for indicator in ['ratelimit', '202', 'rate limit', 'too many requests']):
                    if attempt < max_attempts - 1:
                        continue
                    else:
                        raise Exception("DuckDuckGo rate limit exceeded")
                else:
                    raise e
        
        if not all_results:
            return "No recent web search results found"
        
        # Sort by priority and format results
        all_results.sort(key=lambda x: x['priority'], reverse=True)
        top_results = all_results[:max_results]
        
        formatted_results = []
        for i, result in enumerate(top_results):
            formatted_results.append(
                f"Result {i+1} (Priority Score: {result['priority']}):\n"
                f"Title: {result['title']}\n"
                f"Description: {result['body']}\n"
                f"URL: {result['url']}\n"
            )
        
        return f"RECENT WEB SEARCH RESULTS for '{query}' (DuckDuckGo):\n\n" + "\n".join(formatted_results)
        
    except ImportError:
        raise Exception("duckduckgo-search package not installed")
    except Exception as e:
        raise Exception(f"DuckDuckGo search error: {str(e)}")


def _search_with_yagooglesearch(query: str, max_results: int = 10) -> str:
    """
    Google search using yagooglesearch library with rate limit handling
    """
    try:
        # Try importing with proper error handling
        try:
            import yagooglesearch  # type: ignore
        except ImportError:
            raise Exception("yagooglesearch package not installed. Install with: pip install yagooglesearch")
        
        # Clean query for Google search
        clean_query = query.strip()[:150]
        
        client = yagooglesearch.SearchClient(
            clean_query,
            tbs="li:1",  # Verbatim search
            max_search_result_urls_to_return=max_results * 2,
            http_429_cool_off_time_in_minutes=2,  # Shorter cooloff for faster fallback
            http_429_cool_off_factor=1.5,
            minimum_delay_between_paged_results_in_seconds=2,
            verbosity=1,  # Minimal verbosity
            verbose_output=False,
            yagooglesearch_manages_http_429s=True
        )
        
        client.assign_random_user_agent()
        urls = client.search()
        
        if not urls or "HTTP_429_DETECTED" in urls:
            return "Google search rate limited or no results found"
        
        # Process the URLs to get titles and descriptions
        results = []
        seen_urls = set()
        
        for url in urls[:max_results]:
            if url in seen_urls or _should_skip_url(url):
                continue
            
            seen_urls.add(url)
            
            # Try to get page title and description
            try:
                response = requests.get(url, timeout=5, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                
                if response.status_code == 200:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(response.content, 'html.parser')
                    
                    title = "No title"
                    if soup.title and soup.title.string:
                        title = soup.title.string.strip()
                    
                    description = "No description"
                    meta_desc = soup.find('meta', attrs={'name': 'description'})
                    if meta_desc and hasattr(meta_desc, 'get'):
                        content = meta_desc.get('content')  # type: ignore
                        if isinstance(content, str):
                            description = content.strip()
                    
                    results.append({
                        'title': title,
                        'description': description,
                        'url': url
                    })
                    
            except Exception:
                # If we can't get details, just use the URL
                results.append({
                    'title': url.split('/')[-1] if '/' in url else url,
                    'description': "Description unavailable",
                    'url': url
                })
            
            if len(results) >= max_results:
                break
        
        if not results:
            return "No valid results found from Google search"
        
        # Format results
        formatted_results = []
        for i, result in enumerate(results):
            formatted_results.append(
                f"Result {i+1}:\n"
                f"Title: {result['title']}\n"
                f"Description: {result['description']}\n"
                f"URL: {result['url']}\n"
            )
        
        return f"RECENT WEB SEARCH RESULTS for '{query}' (Google):\n\n" + "\n".join(formatted_results)
        
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
        
        # Major news RSS feeds
        rss_feeds = [
            "https://rss.cnn.com/rss/edition.rss",
            "https://feeds.bbci.co.uk/news/rss.xml",
            "https://www.reuters.com/rssFeed/worldNews",
            "https://rss.ap.org/rss/apf-topnews.rss",
            "https://feeds.npr.org/1001/rss.xml",
            "https://abcnews.go.com/abcnews/topstories",
            "https://feeds.nbcnews.com/nbcnews/public/news"
        ]
        
        all_entries = []
        query_lower = query.lower()
        
        for feed_url in rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                
                for entry in feed.entries:
                    title = entry.get('title', '')
                    summary = entry.get('summary', entry.get('description', ''))
                    link = entry.get('link', '')
                    
                    # Check if query terms appear in title or summary
                    if any(term in (title + summary).lower() for term in query_lower.split()):
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
                            'relevance': _calculate_relevance_score(title + summary, query)
                        })
                        
            except Exception as e:
                print(f"Failed to parse RSS feed {feed_url}: {e}")
                continue
        
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
                f"Description: {entry['summary'][:200]}...\n"
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


def _search_with_alternative_ddg(query: str, max_results: int = 10) -> str:
    """
    Alternative DuckDuckGo implementation using different approach
    """
    try:
        # Try direct requests to DuckDuckGo
        search_url = "https://lite.duckduckgo.com/lite/"
        
        params = {
            'q': query,
            'b': '',
            'kl': 'wt-wt',
            'df': 'm'
        }
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.post(search_url, data=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.content, 'html.parser')
            
            results = []
            result_tables = soup.find_all('table', class_='result')
            
            for table in result_tables[:max_results]:
                title_link = table.find('a', class_='result-link')
                snippet = table.find('td', class_='result-snippet')
                
                if title_link:
                    title = title_link.get_text().strip()
                    url = title_link.get('href', '')
                    description = snippet.get_text().strip() if snippet else "No description"
                    
                    results.append({
                        'title': title,
                        'url': url,
                        'description': description
                    })
            
            if results:
                formatted_results = []
                for i, result in enumerate(results):
                    formatted_results.append(
                        f"Result {i+1}:\n"
                        f"Title: {result['title']}\n"
                        f"Description: {result['description']}\n"
                        f"URL: {result['url']}\n"
                    )
                
                return f"RECENT WEB SEARCH RESULTS for '{query}' (Alternative DDG):\n\n" + "\n".join(formatted_results)
            
        return "No results found with alternative DuckDuckGo"
        
    except Exception as e:
        raise Exception(f"Alternative DuckDuckGo error: {str(e)}")


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
    
    # Clean and limit the query length
    query = original_query.strip()[:150]  # Limit to avoid overly long queries
    
    # Remove problematic characters that might cause search issues
    query = query.replace('```', '').replace('"', '').strip()
    
    # Extract important elements from the query to build a more targeted search
    capitalized_terms = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', query)
    years = re.findall(r'\b(20[0-9]{2})\b', query)
    numbers = re.findall(r'\b\d+\b', query)
    
    # Build a comprehensive query that covers what the multiple queries were trying to achieve
    # Instead of 4 separate API calls, use search operators in a single call
    query_parts = [f'"{query}"']
    
    # Add capitalized terms (likely proper nouns, names, places) for more specific searches
    if capitalized_terms:
        for term in capitalized_terms[:3]:  # Limit to avoid overly long queries
            query_parts.append(f'"{term}"')
    
    # Add year-specific searches if years are found
    if years:
        for year in years[:2]:  # Limit to avoid overly long queries
            query_parts.append(f'({query} {year})')
    elif not any(year in query for year in ['2024', '2025']):
        # Add recency indicators if no years found
        query_parts.append(f'({query} 2024)')
        query_parts.append(f'({query} 2025)')
    
    # Add numbers for more specific searches (could be important figures, statistics, etc.)
    if numbers:
        for num in numbers[:2]:  # Limit to avoid overly long queries
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
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
