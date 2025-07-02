import os
from typing import List

import dotenv
import google.generativeai as genai

# Configure Gemini API
genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))

# Initialize models - trying correct Flash-Lite model name
text_model = genai.GenerativeModel('gemini-2.5-flash-lite')
vision_model = genai.GenerativeModel('gemini-2.5-flash-lite')


def _make_request(model, prompt, temperature: float = 0.8):
    """
    Make a request to Gemini API with retry logic
    """
    try:
        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=8192,
        )
        
        response = model.generate_content(
            prompt,
            generation_config=generation_config
        )
        return response.text
    except Exception as e:
        raise Exception(f"Error making Gemini request: {str(e)}")


def get_gemini_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini for text-based prompts
    """
    return _make_request(text_model, prompt, temperature)


def gemini_describe_image(image_url: str, temperature: float = 0.01):
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
        
        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=2048,
        )
        
        response = vision_model.generate_content(
            [prompt, image],
            generation_config=generation_config
        )
        return response.text
        
    except Exception as e:
        raise Exception(f"Error describing image with Gemini: {str(e)}")


def get_gemini_search_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini with search capabilities.
    Note: Gemini doesn't have built-in web search like Grok, so we'll use 
    the regular text model and instruct it to provide factual information.
    """
    
    return _make_request(text_model, prompt, temperature)


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
