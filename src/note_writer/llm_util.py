import os
from typing import List

import dotenv
from google import genai
from google.genai import types

# Configure Gemini API
client = genai.Client()

# Initialize models - trying correct Flash-Lite model name
# text_model = client.models.get('gemini-2.5-flash-lite')
# vision_model = client.models.get('gemini-2.5-flash-lite')


def _make_request(model, prompt, temperature: float = 0.8):
    """
    Make a request to Gemini API with retry logic
    """
    try:
        print("List of models that support generateContent:\n")
        for m in client.models.list():
            for action in m.supported_actions:
                if action == "generateContent":
                    print(m.name)
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=8192,
            )
        )
        return response.text
    except Exception as e:
        raise Exception(f"Error making Gemini request: {str(e)}")


def get_gemini_response(prompt: str, temperature: float = 0.8):
    """
    Get a response from Gemini for text-based prompts
    """
    return _make_request(prompt, temperature)


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
        
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=[prompt, image],
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=2048,
            )
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
