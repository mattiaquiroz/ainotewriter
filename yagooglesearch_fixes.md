# YaGoogleSearch and Search Logic Fixes

## Issues Identified and Fixed

### 1. **Result Count Bug (Fixed)**
**Problem**: The search showed "found 0 results" even when results were present because the regex pattern `r'Result \d+:'` was looking for "Result 1:" but the actual format was "Result 1 (Priority: X):".

**Fix**: Changed the regex pattern from `r'Result \d+:'` to `r'Result \d+(?:\s*\(Priority:\s*\d+\))?:'` to match both standard and priority formats.

**Location**: `src/note_writer/llm_util.py` line 323

### 2. **Character Encoding Issues (Fixed)**
**Problem**: "Some characters could not be decoded, and were replaced with REPLACEMENT CHARACTER" errors when scraping web pages.

**Fix**: Added proper encoding handling in both `_search_with_yagooglesearch` and `fetch_page_content` functions:
- Try response.encoding first, then fallback to UTF-8
- Use `errors='replace'` to handle problematic characters gracefully
- Added BeautifulSoup encoding fallback

**Locations**: 
- `src/note_writer/llm_util.py` lines 385-400
- `src/note_writer/llm_util.py` lines 990-1015

### 3. **Search Strategy Optimization (Fixed)**
**Problem**: Search wasn't prioritizing recent events (2024-2025) which caused outdated information to be returned.

**Fix**: Reordered search strategies to prioritize recent information:
1. `{query} 2025` (current year first)
2. `{query} 2024` (previous year)
3. `{query} news 2024 OR 2025` (news with recent years)
4. Original query
5. Exact phrase search

**Location**: `src/note_writer/llm_util.py` lines 355-361

### 4. **Overly Strict URL Validation (Fixed)**
**Problem**: Too many valid sources were being filtered out due to aggressive validation, causing "NO VALID SOURCES FOUND" errors.

**Fix**: Made validation more lenient while maintaining quality:
- Added early domain-based filtering to skip low-quality sites
- For non-404/403 errors, still attempt validation instead of immediate rejection
- Changed Gemini validation prompt to be more generous ("BE GENEROUS in validation")
- Only mark as INVALID if clearly broken, not just imperfect

**Location**: `src/note_writer/llm_util.py` lines 1050-1110

### 5. **Rate Limiting (Added)**
**Problem**: Concurrent requests to validate URLs could cause rate limiting or connection issues.

**Fix**: Added 2.4 second delays between URL validation requests to prevent overwhelming servers.

**Location**: `src/note_writer/llm_util.py` lines 1165-1168

### 6. **Enhanced Error Handling (Added)**
**Problem**: When yagooglesearch import failed, the entire search process would crash.

**Fix**: Changed import error handling to gracefully degrade instead of crashing:
- Print warning instead of raising exception
- Return failure message to allow other search engines to try

**Location**: `src/note_writer/llm_util.py` lines 348-351

### 7. **Improved Debug Logging (Added)**
**Problem**: Hard to diagnose issues when search results weren't being used in notes.

**Fix**: Added comprehensive logging throughout the search and validation process:
- Show raw search results length
- Display detailed validation results for each URL
- Show which URLs are being used for note generation
- Added timing information for rate limiting

**Locations**: 
- `src/note_writer/write_note.py` lines 200-205
- `src/note_writer/llm_util.py` lines 1185-1190

## Summary of Improvements

1. **Fixed the "0 results" display bug** - Now correctly shows actual result counts
2. **Resolved character encoding issues** - Proper UTF-8 handling with fallbacks
3. **Optimized search for recent events** - Prioritizes 2024-2025 information
4. **Made URL validation more lenient** - Includes more valid sources while filtering spam
5. **Added rate limiting** - Prevents server overload and connection issues
6. **Enhanced error handling** - Graceful degradation instead of crashes
7. **Added comprehensive logging** - Better debugging and issue diagnosis

## Expected Results

- **Result counts will be accurate** instead of showing "0 results"
- **No more character encoding errors** when scraping web pages
- **More valid sources will be found** for note generation
- **Better handling of recent events** from 2024-2025
- **More detailed logging** for troubleshooting
- **Fewer crashes** due to missing dependencies or network issues

The fixes address the core issues mentioned in the logs while maintaining the quality and reliability of the search functionality.