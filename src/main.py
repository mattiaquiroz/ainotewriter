import argparse
from concurrent.futures import ThreadPoolExecutor
from typing import List

from cnapi.get_api_eligible_posts import get_posts_eligible_for_notes
from cnapi.submit_note import submit_note
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
    note_result: NoteResult = research_post_and_write_note(post)

    log_strings: List[str] = ["-" * 20, f"Post: {post.post_id}", "-" * 20]
    if note_result.post is not None:
        log_strings.append(f"\n*POST TEXT:*\n  {note_result.post.text}\n")
    if note_result.images_summary is not None:
        log_strings.append(f"\n*IMAGE SUMMARY:*\n  {note_result.images_summary}")
    if note_result.error is not None:
        log_strings.append(f"\n*ERROR:* {note_result.error}")
    if note_result.refusal:
        log_strings.append(f"\n*REFUSAL:* {note_result.refusal}")
    if note_result.note:
        log_strings.append(f"\n*NOTE:*\n  {note_result.note.note_text}\n")
        log_strings.append(
            f"\n*MISLEADING TAGS:*\n  {[tag.value for tag in note_result.note.misleading_tags]}\n"
        )

    # Only add to Gist if we have a note AND no refusal AND no error
    if note_result.note is not None and not note_result.refusal and not note_result.error and not dry_run:
        try:
            submit_note(
                note=note_result.note,
                test_mode=True,
                verbose_if_failed=False,
            )
            log_strings.append("\n*SUCCESSFULLY SUBMITTED NOTE*\n")
            # Add the post ID to the Gist to track that it's been processed
            if add_processed_post_id(str(post.post_id)):
                log_strings.append(f"*ADDED TO GIST*: Post {str(post.post_id)} successfully added to processed list\n")
            else:
                log_strings.append("*GIST WARNING*: Failed to add post ID to processed list\n")
        except Exception as e:
            error_str = str(e)
            if "already created a note" in error_str.lower():
                log_strings.append("\n*ALREADY HAVE NOTE*: We already wrote a note on this post; moving on.\n")
                # Still add to Gist since we know it's been processed
                if add_processed_post_id(str(post.post_id)):
                    log_strings.append(f"*ADDED TO GIST*: Post {str(post.post_id)} successfully added to processed list\n")
                else:
                    log_strings.append("*GIST WARNING*: Failed to add post ID to processed list\n")
            else:
                log_strings.append(f"\n*ERROR SUBMITTING NOTE*: {error_str}\n")
    # If there's a refusal, error, or no note was generated, we DON'T add to Gist
    # This allows the post to be retried in future runs
    print("".join(log_strings) + "\n")


def main(
    num_posts: int = 20,
    dry_run: bool = False,
    concurrency: int = 1,
):
    """
    Get up to `num_posts` recent posts eligible for notes and write notes for them.
    If `dry_run` is True, do not submit notes to the API, just print them to the console.
    """

    # Get posts we've already processed from the Gist to save expensive API calls
    print("Fetching processed post IDs from Gist to avoid duplicate work...")
    processed_post_ids = get_processed_post_ids()
    print(f"Found {len(processed_post_ids)} posts already processed")
    
    # Get eligible posts
    eligible_posts: List[Post] = get_posts_eligible_for_notes(max_results=num_posts)
    print(f"Found {len(eligible_posts)} recent posts eligible for notes")
    
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
            for future in futures:
                future.result()
    else:
        for post in new_posts:
            _worker(post, dry_run)
    print("Done.")


if __name__ == "__main__":
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(description="Run note‑writing bot once.")
    parser.add_argument(
        "--num-posts", type=int, default=20, help="Number of posts to process"
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
