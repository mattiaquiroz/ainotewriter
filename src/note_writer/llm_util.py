import os
import time
from typing import List

import dotenv
from google import genai
from google.genai import types

# Configure Gemini API
client = genai.Client()

# Rate limiting: Gemini free tier allows 15 requests per minute
# We'll space requests to stay safely under this limit
_last_request_time = 0
_min_request_interval = 4.5  # 4.5 seconds between requests (13 requests per minute to be safe)

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
    
    for attempt in range(max_retries + 1):
        try:
            return api_call_func()
        except Exception as e:
            error_str = str(e)
            
            # Handle rate limiting (429 errors) and service unavailable (503 errors)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "503" in error_str or "UNAVAILABLE" in error_str:
                if attempt < max_retries:
                    if "503" in error_str or "UNAVAILABLE" in error_str:
                        # For service unavailable, use shorter initial wait time
                        wait_time = 10 if attempt == 0 else min(30 * (2 ** (attempt - 1)), 120)
                        print(f"Service unavailable (503). Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                    else:
                        # For rate limiting, use longer wait times
                        wait_time = 60  # Default to 60 seconds for rate limits
                        if "retryDelay" in error_str and "55s" in error_str:
                            wait_time = 55
                        elif attempt > 0:
                            wait_time = min(60 * (2 ** attempt), 300)  # Exponential backoff, max 5 minutes
                        print(f"Rate limit hit. Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                    
                    time.sleep(wait_time)
                    continue
                else:
                    if "503" in error_str or "UNAVAILABLE" in error_str:
                        raise Exception(f"Service unavailable after {max_retries} retries. "
                                      f"The Gemini API is currently overloaded. Please try again later.")
                    else:
                        raise Exception(f"Rate limit exceeded after {max_retries} retries. "
                                      f"Gemini API free tier allows 15 requests per minute. "
                                      f"Consider upgrading your plan or waiting before retrying.")
            
            # For other errors, don't retry
            raise Exception(f"Error making Gemini request: {str(e)}")
    
    # Should never reach here
    raise Exception("Unexpected error in _retry_with_backoff")

def _make_request(prompt, temperature: float = 0.8, max_retries: int = 3):
    """
    Make a request to Gemini API with retry logic for rate limiting
    """
    def api_call():
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite-preview-06-17',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=8192,
            )
        )
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
                model='gemini-2.5-flash-lite-preview-06-17',
                contents=[prompt, image],
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=2048,
                )
            )
            return response.text
        
        # Use shared retry logic
        return _retry_with_backoff(api_call, max_retries)
        
    except Exception as e:
        raise Exception(f"Error describing image with Gemini: {str(e)}")


def get_gemini_search_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini with search capabilities.
    Note: Gemini doesn't have built-in web search like Grok, so we'll use 
    the regular text model and instruct it to provide factual information.
    """
    
    return _make_request(prompt, temperature)


# Maintain backwards compatibility with existing function names
def get_grok_response(prompt: str, temperature: float = 0.8):
    """Backwards compatibility wrapper for get_gemini_response"""
    return get_gemini_response(prompt, temperature)


def grok_describe_image(image_url: str, temperature: float = 0.01):
    """Backwards compatibility wrapper for gemini_describe_image"""
    return gemini_describe_image(image_url, temperature)


def get_grok_live_search_response(prompt: str, temperature: float = 0.8):
    """Backwards compatibility wrapper for get_gemini_search_response"""
    return get_gemini_search_response(prompt, temperature)


if __name__ == "__main__":
    dotenv.load_dotenv()
    print(
        get_gemini_search_response(
            "Provide me a digest of world news in the last 2 hours. Please respond with links to each source next to the claims that the source supports."
        )
    )
