import re
from data_models import NoteResult, Post, ProposedMisleadingNote
from note_writer.llm_util import (
    get_gemini_search_response,
    get_gemini_response,
    gemini_describe_image,
)
from note_writer.misleading_tags import get_misleading_tags


def _ensure_urls_have_protocol(text: str) -> str:
    """Ensure all URLs in the text have https:// prefix for API compliance"""
    # Simple pattern to find domain.com style URLs without protocol
    url_pattern = r'\b([a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9]*\.)+[a-zA-Z]{2,}(?:/[^\s]*)?'
    
    def add_https_if_needed(match):
        url = match.group(0)
        # Check if the URL already has a protocol by looking at the text before it
        start_pos = match.start()
        # Look for protocol before the match
        before_match = text[max(0, start_pos-10):start_pos]
        if before_match.endswith('://') or before_match.endswith('http://') or before_match.endswith('https://'):
            return url
        return f'https://{url}'
    
    return re.sub(url_pattern, add_https_if_needed, text)

def _get_prompt_for_note_writing(post: Post, images_summary: str, search_results: str):
    return f"""You will be given a post on X (formerly Twitter), a summary of any images, and live search results. 
        Your task is to determine whether the post is misleading and if it merits a Community Note.

        Instructions:

        - If the post is misleading and there is strong, up-to-date, verifiable evidence to support a correction, write a concise Community Note (280 characters max, not counting URLs).
        - The note must:
        - Be written in a clear, neutral, and professional tone (no emojis, hashtags, or preambles like "Community Note:")
        - Include at least one working, trustworthy URL as a source. Do not write "[Source]" — just include the plain URL.
        - Prefer recent, non-partisan sources that would be found trustworthy across political perspectives.
        - If the post is not misleading or does not contain concrete, fact-checkable claims, respond with:
        - "NO NOTE NEEDED."
        - If the post may be misleading but the available evidence is outdated, broken (e.g. 404 links), or insufficient to confidently write a correction, respond with:
        - "NOT ENOUGH EVIDENCE TO WRITE A GOOD COMMUNITY NOTE."

        Only write a note if you're highly confident that:
        - The post is misleading,
        - The correction is well-supported by evidence,
        - The correction would likely be seen as helpful and trustworthy by readers across political views.

        Do not write notes about predictions or speculative statements. Ignore outdated or incorrect source content (e.g. referring to past administrations or candidates when context has changed).

        Post text:
        ```
        {post.text}
        ```

        Summary of images in the post:
        ```
        {images_summary}
        ```

        Live search results:
        ```
        {search_results}
        ```
    """

def _get_prompt_for_live_search(post: Post, images_summary: str = ""):
    return f"""Below is a post on X. Conduct research to determine if the post is potentially misleading.
        Your response MUST include URLs/links directly in the text next to the claim they support. Do NOT include any unnecessary characters like "[Source]", just provide the plain URL.
        Only cite sources that are reliable, up-to-date, and accessible (avoid 404 errors or outdated information). If a source is broken or clearly outdated (e.g., refers to past events that are no longer relevant), do NOT use it.
        Post text:
        ```
        {post.text}
        ```

        Summary of images in the post:
        ```
        {images_summary}
        ```
    """

def _summarize_images(post: Post) -> str:
    """
    Summarize images, if they exist. Abort if video or other unsupported media type.
    """
    images_summary = ""
    for i, media in enumerate(post.media):
        if media.media_type == "photo":
            image_description = gemini_describe_image(media.url)
            images_summary += f"Image {i}: {image_description}\n\n"
        elif media.media_type == "video":
            raise ValueError("Video not supported yet")
        else:
            raise ValueError(f"Unsupported media type: {media.type}")
    return images_summary


def research_post_and_write_note(
    post: Post,
) -> NoteResult:
    try:
        images_summary = _summarize_images(post)
    except ValueError as e:
        return NoteResult(post=post, error=str(e))

    search_prompt = _get_prompt_for_live_search(post, images_summary)
    search_results = get_gemini_search_response(search_prompt)
    
    # Handle case where search results are None
    if search_results is None:
        return NoteResult(post=post, error="Failed to get search results from Gemini API")
    
    note_prompt = _get_prompt_for_note_writing(post, images_summary, search_results)

    note_or_refusal_str = get_gemini_response(note_prompt)

    # Handle case where Gemini API returns None due to errors
    if note_or_refusal_str is None:
        return NoteResult(post=post, error="Failed to get response from Gemini API")

    if ("NO NOTE NEEDED" in note_or_refusal_str) or (
        "NOT ENOUGH EVIDENCE TO WRITE A GOOD COMMUNITY NOTE" in note_or_refusal_str
    ):
        return NoteResult(post=post, refusal=note_or_refusal_str)

    # Ensure URLs in the note have proper protocol
    formatted_note_text = _ensure_urls_have_protocol(note_or_refusal_str)

    misleading_tags = get_misleading_tags(post, images_summary, formatted_note_text)

    return NoteResult(
        post=post,
        note=ProposedMisleadingNote(
            post_id=str(post.post_id),  # Convert int to str for API compliance
            note_text=formatted_note_text,
            misleading_tags=misleading_tags,
        ),
    )
