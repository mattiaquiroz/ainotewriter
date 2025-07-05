import re
from data_models import NoteResult, Post, ProposedMisleadingNote
from note_writer.llm_util import (
    get_gemini_search_response,
    get_gemini_response,
    gemini_describe_image,
    verify_and_filter_links,
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

        CURRENT DATE CONTEXT: Today is 2025. Pay special attention to claims about recent events from late 2024 through 2025.
        
        CRITICAL: Your training data may be outdated. ALWAYS prioritize information from live search results and web sources over your training knowledge, especially for recent events, political developments, elections, policy changes, and current affairs.

        Instructions:

        - If the post is misleading and there is strong, up-to-date, verifiable evidence to support a correction, write a concise Community Note (280 characters max, not counting URLs).
        - The note must:
        - Be written in a clear, neutral, and professional tone (no emojis, hashtags, or preambles like "Community Note:")
        - Include at least one working, trustworthy URL as a source. Do not write "[Source]" — just include the plain URL.
        - Use ONLY recent, non-partisan sources that would be found trustworthy across political perspectives.
        - CRITICAL: Only cite URLs that are marked as "VERIFIED VALID SOURCES" in the search results. Do not use any broken, 404, or invalid sources.
        - Ensure all factual claims in your note are current and accurate as of today's date (2025).
        - If the post is not misleading or does not contain concrete, fact-checkable claims, respond with:
        - "NO NOTE NEEDED."
        - If the post may be misleading but the available evidence is outdated, broken (e.g. 404 links), or insufficient to confidently write a correction, respond with:
        - "NOT ENOUGH EVIDENCE TO WRITE A GOOD COMMUNITY NOTE."

        CRITICAL: Only write a note if you're highly confident that:
        - The post is misleading,
        - The correction is well-supported by current, verifiable evidence,
        - The correction would likely be seen as helpful and trustworthy by readers across political views,
        - All information in your note is up-to-date and contextually accurate for 2025.

        SPECIAL WARNINGS FOR RECENT EVENTS (2024-2025):
        - Be extremely cautious about claims involving recent legislation, bills, or political events
        - If your knowledge seems outdated about a claimed recent event, acknowledge this uncertainty
        - For recent political events: prioritize the most current sources available in the search results
        - If the search results contain conflicting information about recent events, lean toward "NOT ENOUGH EVIDENCE"
        - Be humble about the limitations of your training data for very recent events

        Do not write notes about predictions or speculative statements. 
        REJECT any source content that appears outdated or contextually incorrect (e.g. referring to past administrations, outdated positions, expired legislation, or changed circumstances).
        REJECT any source that returned 404, 403, or other errors - do not use information from broken or inaccessible sources.
        REJECT any eliminated/deleted Twitter/X posts as sources.
        Verify that names, titles, dates, and context are all current and accurate.

        Post text:
        ```
        {post.text or "[No text content]"}
        ```

        Summary of images in the post:
        ```
        {images_summary or "[No images]"}
        ```

        Live search results:
        ```
        {search_results}
        ```
    """

def _get_prompt_for_live_search(post: Post, images_summary: str = ""):
    return f"""Below is a post on X. Conduct research to determine if the post is potentially misleading.
        
        CURRENT DATE CONTEXT: Today is 2025. Be especially thorough when fact-checking recent events from late 2024 through 2025, as this information may be very recent.
        
        IMPORTANT: Your knowledge may be outdated for recent events. You must search for and rely on current web sources rather than your training data for any claims about events in 2024-2025.
        
        CRITICAL REQUIREMENTS for sources:
        - Your response MUST include specific, direct URLs/links next to the claims they support
        - Only cite sources from reputable news outlets, government websites, academic institutions, or well-established organizations
        - Ensure all URLs are complete, properly formatted, and likely to be accessible
        - For events from 2024-2025: PRIORITIZE the most recent sources available (within days/weeks if possible)
        - For recent political events, bills, legislation: Search for the most current information from official government sources
        - Verify the information is still current and relevant to today's context (2025)
        - Do NOT include generic domain names or incomplete URLs
        - Do NOT include any formatting like "[Source]" - just provide the plain, complete URL
        - Do NOT cite outdated information that may no longer be accurate
        - AVOID Twitter/X links as primary sources - prefer mainstream news or official sources

        SPECIAL ATTENTION FOR RECENT EVENTS:
        - If the post mentions specific bills, legislation, or political events from 2024-2025, search extensively for the most current information
        - Check multiple recent news sources to verify if claimed events actually occurred
        - Look for official government announcements, press releases, or congressional records
        - Be aware that your knowledge may be outdated for very recent events

        Focus on finding sources that would be considered trustworthy across different political perspectives.
        If you cannot find reliable, current, specific sources for the claims in the post, say so explicitly.
        Be especially careful to verify that any dates, names, positions, or context mentioned are still accurate.

        Post text:
        ```
        {post.text or "[No text content]"}
        ```

        Summary of images in the post:
        ```
        {images_summary or "[No images]"}
        ```
    """

def _summarize_images(post: Post) -> str:
    """
    Summarize images, if they exist. Abort if video or other unsupported media type.
    """
    images_summary = ""
    for i, media in enumerate(post.media):
        if media.media_type == "photo":
            if not media.url:
                images_summary += f"Image {i}: [No URL available for image]\n\n"
                continue
            try:
                image_description = gemini_describe_image(media.url)
                if image_description and image_description.strip():
                    images_summary += f"Image {i}: {image_description.strip()}\n\n"
                else:
                    images_summary += f"Image {i}: [Unable to analyze image]\n\n"
            except Exception as e:
                images_summary += f"Image {i}: [Error analyzing image: {str(e)[:100]}]\n\n"
        elif media.media_type == "video":
            raise ValueError("Video not supported yet")
        else:
            raise ValueError(f"Unsupported media type: {media.media_type}")
    return images_summary


def research_post_and_write_note(
    post: Post,
) -> NoteResult:
    # Check if post has meaningful content (text or media)
    if (not post.text or not post.text.strip()) and (not post.media or len(post.media) == 0):
        return NoteResult(post=post, refusal="NO NOTE NEEDED: Post appears to be empty with no text content or media.")
    
    try:
        images_summary = _summarize_images(post)
    except ValueError as e:
        return NoteResult(post=post, error=str(e))

    search_prompt = _get_prompt_for_live_search(post, images_summary)
    search_results = get_gemini_search_response(search_prompt)
    
    # Handle case where search results are None
    if search_results is None:
        return NoteResult(post=post, error="Failed to get search results from Gemini API")
    
    print(f"🔍 Raw search results received: {len(search_results)} characters")
    
    # Verify and filter links in search results
    filtered_search_results, valid_urls = verify_and_filter_links(search_results, post.text)
    
    # If no valid sources found, cancel the note
    if filtered_search_results is None or len(valid_urls) == 0:
        print("❌ No valid sources found for note generation")
        return NoteResult(
            post=post, 
            refusal="NO VALID SOURCES FOUND: All links in search results were either broken, irrelevant, or inaccessible. Cannot write a reliable Community Note without credible sources."
        )
    
    print(f"✅ Using filtered search results with {len(valid_urls)} valid sources for note generation")
    
    # Use filtered search results for note writing
    note_prompt = _get_prompt_for_note_writing(post, images_summary, filtered_search_results)

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
        images_summary=images_summary,
    )
