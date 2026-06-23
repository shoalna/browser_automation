# browser_automation

A fluent, ergonomic browser automation library built on [Playwright](https://playwright.dev/python/).

Chain actions in plain, readable Python — no `async/await`, no boilerplate.

```python
from browser_automation import Browser

result = (
    Browser()
    .goto("https://news.ycombinator.com")
    .extract_all(".athing .titleline > a", name="headlines")
    .run()
)

for headline in result["headlines"]:
    print(headline)
```

---

## Installation

```bash
pip install browser_automation
```

Browser binaries are downloaded automatically on first use — no extra setup needed.

---

## Quick examples

### Scrape a single value

```python
result = (
    Browser()
    .goto("https://example.com")
    .extract("h1", name="title")
    .run()
)

print(result["title"])  # "Example Domain"
```

### Fill a form and submit

```python
Browser()
    .goto("https://example.com/search")
    .type("#query", "playwright python")
    .click("[type=submit]")
    .wait()   # wait for network idle after submission
    .run()
```

### Handle an optional element (cookie banner)

```python
result = (
    Browser()
    .goto("https://example.com")
    .if_exists("#cookie-banner", lambda b: b.click("#accept-all"))
    .extract("h1", name="title")
    .run()
)
```

### Paginate until there are no more pages

```python
result = (
    Browser()
    .goto("https://example.com/results")
    .repeat_until(
        "#next-page[disabled]",
        lambda b: b.click("#next-page").wait(),
    )
    .extract_all(".result-title", name="titles")
    .run()
)

print(f"Scraped {len(result['titles'])} results")
```

### Load more content N times then scrape

```python
result = (
    Browser()
    .goto("https://example.com/feed")
    .repeat(5, lambda b: b.click("#load-more").wait())
    .extract_all(".post-title", name="titles")
    .run()
)
```

### Extract an attribute instead of text

```python
result = (
    Browser()
    .goto("https://example.com")
    .extract("link[rel=canonical]", name="canonical", attr="href")
    .extract_all("a.nav", name="nav_links", attr="href")
    .run()
)

print(result["canonical"])   # "https://example.com/"
print(result["nav_links"])   # ["https://example.com/about", ...]
```

### Iterate over a list of elements

```python
rows = []
Browser()
    .goto("https://example.com/table")
    .each("table tr", lambda b, el: rows.append(el.inner_text()))
    .run()
```

### Scroll to trigger lazy-loaded content

```python
result = (
    Browser()
    .goto("https://example.com/gallery")
    .scroll()          # scroll to bottom
    .wait(seconds=1)   # wait for images to load
    .scroll()          # scroll again for more
    .extract_all("img.gallery-item", name="images", attr="src")
    .run()
)
```

### Persist a login session

```python
# Log in once and save session
Browser(headless=False, state_file="session.json")
    .goto("https://example.com/login")
    .type("#email", "user@example.com")
    .type("#password", "secret")
    .click("[type=submit]")
    .wait()
    .run()

# Reuse saved session on every subsequent run
result = (
    Browser(state_file="session.json")
    .goto("https://example.com/dashboard")
    .extract("h1", name="greeting")
    .run()
)
```

### Debug with a visible browser and screenshots

```python
result = (
    Browser(headless=False, screenshot_on_failure=True)
    .goto("https://example.com")
    .screenshot("before_click.png")
    .click("#some-button")
    .screenshot()           # auto-named with timestamp
    .extract("h1", name="title")
    .run()
)
```

### Handle soft failures

```python
result = (
    Browser()
    .goto("https://example.com")
    .click("#maybe-missing", optional=True)   # won't raise if absent
    .extract("h1", name="title")
    .run()
)

if not result.ok:
    for error in result.errors:
        print("Soft failure:", error)
```

### Access the raw Playwright page

```python
browser = Browser()
browser.goto("https://example.com")
browser.page.pdf(path="output.pdf")   # raw Playwright Page object
browser.run()
```

---

## Browser constructor options

| Parameter | Type | Default | Description |
|---|---|---|---|
| `browser` | `str` | `"chromium"` | Engine: `"chromium"`, `"firefox"`, `"webkit"` |
| `headless` | `bool` | `True` | Hide the browser window |
| `viewport` | `tuple[int,int]` | `(1280, 720)` | Window size in pixels |
| `user_agent` | `str \| None` | `None` | Override the User-Agent header |
| `http_credentials` | `tuple[str, str] \| None` | `None` | `(username, password)` for HTTP Basic Auth |
| `state_file` | `str \| None` | `None` | Path to session JSON (load + save) |
| `screenshot_on_failure` | `bool` | `False` | Auto-screenshot on action errors |
| `verbose` | `bool` | `False` | Enable DEBUG-level logging |

---

## API reference

### Navigation

| Method | Description |
|---|---|
| `.goto(url)` | Navigate to a URL |

### Interactions

| Method | Description |
|---|---|
| `.click(selector, optional=False)` | Click the first matching element |
| `.type(selector, text, optional=False)` | Clear and fill an input |
| `.select(selector, value, optional=False)` | Select a `<select>` option by value |
| `.hover(selector, optional=False)` | Hover over an element |

### Scroll

| Method | Description |
|---|---|
| `.scroll()` | Scroll to page bottom |
| `.scroll(selector)` | Scroll element into viewport |
| `.scroll(y=N)` | Scroll down N pixels |

### Wait

| Method | Description |
|---|---|
| `.wait(selector)` | Wait for element to be visible |
| `.wait(seconds=N)` | Pause for N seconds |
| `.wait()` | Wait for network idle |

### Extraction

| Method | Description |
|---|---|
| `.extract(selector, name=, attr=None)` | Extract one value (text or attribute) |
| `.extract_all(selector, name=, attr=None)` | Extract all matching values as a list |

### Capture

| Method | Description |
|---|---|
| `.screenshot(filename=None)` | Save screenshot (auto-named if omitted) |
| `.save_html(filename=None)` | Save full live DOM as HTML (auto-named if omitted) |

### Debug

| Method | Description |
|---|---|
| `.pause()` | Open Playwright Inspector and halt until Resume is clicked (skipped in headless mode) |

### Iframe

| Method | Description |
|---|---|
| `.enter_frame(selector, optional=False)` | Enter "frame mode" — scope subsequent actions to the iframe (resolved once; nests) |
| `.exit_frame()` | Exit one frame level, back to the parent frame or page |
| `.within_frame(selector, fn)` | Sugar for `enter_frame` → `fn(browser)` → `exit_frame` |
| `.frame(selector)` | Return the raw Playwright `Frame` object (escape hatch) |

`enter_frame` / `exit_frame` resolve the iframe a single time and keep it active across many actions — preferred over repeated `within_frame` calls on the same iframe. Page-level actions (`press_key`, `screenshot`, `pause`, `goto`) always target the real page, even in frame mode. Pair every `enter_frame` with an `exit_frame`; frame mode resets at the end of `run()`.

```python
result = (
    Browser()
    .goto(url)
    .wait("iframe#editor")
    .enter_frame("iframe#editor")
    .type("#title", "Hello")
    .click("#bold")
    .extract("h1", name="heading")
    .exit_frame()
    .extract("title", name="page_title")
    .run()
)
```

### Control flow

| Method | Description |
|---|---|
| `.if_exists(selector, fn)` | Run `fn(browser)` only if selector found |
| `.do(fn)` | Run `fn(store, browser)` mid-workflow — read extracted values and branch |
| `.each(selector, fn)` | Run `fn(browser, locator)` per element |
| `.repeat(n, fn)` | Run `fn(browser)` exactly n times |
| `.repeat_until(selector, fn, max_iterations=100)` | Run `fn(browser)` until selector appears |

### Terminal

| Method | Description |
|---|---|
| `.run()` | Execute all steps, return `Result` |

### Result object

```python
result["key"]     # extracted value by name
result.data       # dict of all extracted values
result.errors     # list of soft failure messages
result.ok         # True if no soft failures
```

---

## Logging

`browser_automation` uses Python's standard `logging` module under the
`browser_automation` logger. To see all steps:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

Or set `verbose=True` on the `Browser` constructor for the same effect scoped
to this library only.
