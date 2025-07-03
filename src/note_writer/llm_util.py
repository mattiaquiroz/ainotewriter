import os
import time
from typing import List

import dotenv
from google import genai
from google.genai import types

# Configure Gemini API
client = genai.Client()

# Rate limiting: Gemini free tier allows 15 requests per minute
_last_request_time = 0
_min_request_interval = 13  # 13 seconds between requests (4 requests per minute to be safe)

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
            
            # Handle retryable errors:
            # - Rate limiting (429 errors)
            # - Service unavailable (503 errors) 
            # - None response text (content filtering or temporary model issues)
            # - Other temporary API issues
            is_retryable = any([
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
                raise Exception(f"Error making Gemini request: {str(e)}")
    
    # Should never reach here
    raise Exception("Unexpected error in _retry_with_backoff")

def _make_request(prompt, temperature: float = 0.8, max_retries: int = 3):
    """
    Make a request to Gemini API with retry logic for rate limiting
    """
    def api_call():
        response = client.models.generate_content(
            model='gemini-2.5-pro',
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
            
            # Check if there are any candidates
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'finish_reason'):
                    error_details.append(f"finish_reason: {candidate.finish_reason}")
                if hasattr(candidate, 'safety_ratings'):
                    error_details.append(f"safety_ratings: {candidate.safety_ratings}")
            
            # Check for prompt feedback
            if hasattr(response, 'prompt_feedback'):
                if hasattr(response.prompt_feedback, 'block_reason'):
                    error_details.append(f"block_reason: {response.prompt_feedback.block_reason}")
                if hasattr(response.prompt_feedback, 'safety_ratings'):
                    error_details.append(f"prompt_safety_ratings: {response.prompt_feedback.safety_ratings}")
            
            error_msg = "Gemini API returned None response text"
            if error_details:
                error_msg += f" ({'; '.join(error_details)})"
            
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
                model='gemini-2.5-pro',
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
                
                # Check if there are any candidates
                if hasattr(response, 'candidates') and response.candidates:
                    candidate = response.candidates[0]
                    if hasattr(candidate, 'finish_reason'):
                        error_details.append(f"finish_reason: {candidate.finish_reason}")
                    if hasattr(candidate, 'safety_ratings'):
                        error_details.append(f"safety_ratings: {candidate.safety_ratings}")
                
                # Check for prompt feedback
                if hasattr(response, 'prompt_feedback'):
                    if hasattr(response.prompt_feedback, 'block_reason'):
                        error_details.append(f"block_reason: {response.prompt_feedback.block_reason}")
                    if hasattr(response.prompt_feedback, 'safety_ratings'):
                        error_details.append(f"prompt_safety_ratings: {response.prompt_feedback.safety_ratings}")
                
                error_msg = "Gemini API returned None response text for image description"
                if error_details:
                    error_msg += f" ({'; '.join(error_details)})"
                
                print(f"DEBUG: {error_msg}")
                print(f"DEBUG: Full response object: {response}")
                
                raise Exception(error_msg)
            
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


if __name__ == "__main__":
    dotenv.load_dotenv()
    print(
        get_gemini_search_response(
            "Provide me a digest of world news in the last 2 hours. Please respond with links to each source next to the claims that the source supports."
        )
    )
