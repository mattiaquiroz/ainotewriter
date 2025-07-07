import argparse
from concurrent.futures import ThreadPoolExecutor
from typing import List

from cnapi.get_api_eligible_posts import get_posts_eligible_for_notes
from cnapi.submit_note import submit_note
from cnapi.submit_note import get_notes_written_by_user
from cnapi.gist_util import get_processed_post_ids, add_processed_post_id
from data_models import NoteResult, Post
import dotenv
from note_writer.write_note import research_post_and_write_note


def _worker(
    post: Post,
    dry_run: bool = False,
):
    """
    Fetch and try to write and submit a note for one post.
    If `dry_run` is True, do not submit notes to the API, just print them to the console.
    """
    try:
        print(f"\n--------------------Post: {post.post_id}--------------------")
        
        note_result: NoteResult = research_post_and_write_note(post)

        log_strings: List[str] = []
        if note_result.post is not None:
            # Show if post is empty or just whitespace
            text_content = note_result.post.text or "[No text content]"
            if not note_result.post.text or not note_result.post.text.strip():
                text_content = "[Empty post]"
            log_strings.append(f"*POST TEXT:*\n  {text_content}\n")
        if note_result.images_summary is not None:
            # Show if image summary is empty
            images_content = note_result.images_summary.strip() if note_result.images_summary else "[No images or failed to analyze]"
            log_strings.append(f"\n*IMAGE SUMMARY:*\n  {images_content}")
        if note_result.error is not None:
            log_strings.append(f"\n*ERROR:* {note_result.error}")
        if note_result.refusal:
            log_strings.append(f"\n*REFUSAL:* {note_result.refusal}")
        if note_result.note:
            log_strings.append(f"\n*NOTE:*\n  {note_result.note.note_text}\n")
            log_strings.append(
                f"\n*MISLEADING TAGS:*\n  {[tag.value for tag in note_result.note.misleading_tags]}\n"
            )

        # Check if this is a permanent condition that should be added to Gist
        should_add_to_gist = False
        
        # Only add to Gist if we have a note AND no refusal AND no error
        if note_result.note is not None and not note_result.refusal and not note_result.error and not dry_run:
            try:
                submit_note(
                    note=note_result.note,
                    test_mode=True,
                    verbose_if_failed=False,
                )
                log_strings.append("\n*SUCCESSFULLY SUBMITTED NOTE*")
                should_add_to_gist = True
            except Exception as e:
                error_str = str(e)
                if "already created a note" in error_str.lower():
                    log_strings.append("\n*ALREADY HAVE NOTE*: We already wrote a note on this post; moving on.")
                    should_add_to_gist = True
                else:
                    log_strings.append(f"\n*ERROR SUBMITTING NOTE*: {error_str}")
        
        # Also add to Gist for permanent conditions that won't change on retry
        elif not dry_run:
            if note_result.error and "Video not supported yet" in note_result.error:
                log_strings.append("\n*PERMANENT ERROR*: Video not supported - adding to processed list")
                should_add_to_gist = True
            elif note_result.refusal and "NO NOTE NEEDED" in note_result.refusal:
                log_strings.append("\n*PERMANENT REFUSAL*: No note needed - adding to processed list")
                should_add_to_gist = True
        
        # Add to Gist if we determined it should be added
        if should_add_to_gist:
            if add_processed_post_id(str(post.post_id)):
                log_strings.append(f"*ADDED TO GIST*: Post {str(post.post_id)} successfully added to processed list")
            else:
                log_strings.append("*GIST WARNING*: Failed to add post ID to processed list")
        
        # Print all the log strings for this post
        print("\n".join(log_strings))
        
    except Exception as e:
        # Catch any unhandled exceptions to prevent program termination
        print(f"\n--------------------Post: {post.post_id}--------------------")
        print(f"*CRITICAL ERROR*: Unhandled exception occurred while processing post: {str(e)}")
        print(f"*SKIPPING POST*: Moving to next post to avoid program termination")
        # Don't add to gist for unexpected errors as they might be temporary
        
        # Optionally log the full traceback for debugging
        import traceback
        print(f"*FULL TRACEBACK*:\n{traceback.format_exc()}")


def main(
    num_posts: int = 5,
    dry_run: bool = False,
    concurrency: int = 1,
):
    """
    Get up to `num_posts` recent posts eligible for notes and write notes for them.
    If `dry_run` is True, do not submit notes to the API, just print them to the console.
    """
    try:
        # Get posts we've already processed from the Gist to save expensive API calls
        print("Fetching processed post IDs from Gist to avoid duplicate work...")
        processed_post_ids = get_processed_post_ids()
        print(f"Found {len(processed_post_ids)} posts already processed")
        
        # Get eligible posts
        eligible_posts: List[Post] = get_posts_eligible_for_notes(max_results=num_posts)
        print(f"Found {len(eligible_posts)} recent posts eligible for notes")

        notes_written_by_user = get_notes_written_by_user(max_results=num_posts)

        for note in notes_written_by_user.get("data", []):
            print(f"Note ID: {note.get('id')}")
            print(f"Status: {note.get('status')}")
            print(f"Test Result: {note.get('test_result')}")
            print("--------------------------------")

        return
        
        # Filter out posts we've already processed
        new_posts = [post for post in eligible_posts if str(post.post_id) not in processed_post_ids]
        skipped_count = len(eligible_posts) - len(new_posts)
        
        print(f"  Eligible Post IDs: {', '.join([str(post.post_id) for post in eligible_posts])}")
        if skipped_count > 0:
            skipped_ids = [str(post.post_id) for post in eligible_posts if str(post.post_id) in processed_post_ids]
            print(f"  🚀 SKIPPED {skipped_count} posts (already processed): {', '.join(skipped_ids)}")
            print(f"  💰 SAVED EXPENSIVE AI CALLS: Avoided {skipped_count} Gemini API calls!")
        print(f"📝 Processing {len(new_posts)} posts\n")
        
        if len(new_posts) == 0:
            print("No posts to process - we already have processed all eligible posts!")
            return

        if concurrency > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [
                    executor.submit(_worker, post, dry_run) for post in new_posts
                ]
                # Wait for all futures to complete and handle any exceptions
                for i, future in enumerate(futures):
                    try:
                        future.result()  # This will raise any exception that occurred in the worker
                    except Exception as e:
                        print(f"Exception in worker thread {i}: {e}")
                        # Continue processing other posts
        else:
            for post in new_posts:
                _worker(post, dry_run)
        
        print("\nDone.")
        
    except Exception as e:
        print(f"\nCRITICAL ERROR in main function: {str(e)}")
        import traceback
        print(f"Full traceback:\n{traceback.format_exc()}")
        # Exit with error code but gracefully
        exit(1)


if __name__ == "__main__":
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(description="Run note‑writing bot once.")
    parser.add_argument(
        "--num-posts", type=int, default=5, help="Number of posts to process"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not submit notes to the API, just print them to the console",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent tasks to run",
    )
    args = parser.parse_args()
    main(
        num_posts=args.num_posts,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
    )
