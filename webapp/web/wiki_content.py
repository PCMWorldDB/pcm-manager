"""File-backed wiki content for the in-app documentation."""
import html
import re
from pathlib import Path

import yaml
from markupsafe import Markup


WIKI_ROOT = Path(__file__).resolve().parents[1] / 'wiki'
WIKI_INDEX_PATH = WIKI_ROOT / 'wiki_index.yaml'
WIKI_PAGES_ROOT = WIKI_ROOT / 'pages'


def load_wiki_sections():
    """Load the wiki tree from the editable wiki index."""
    with WIKI_INDEX_PATH.open(encoding='utf-8') as index_file:
        data = yaml.safe_load(index_file) or {}
    return hydrate_wiki_paths(data.get('sections', []))


def find_wiki_section(section_slug, wiki_sections=None):
    wiki_sections = wiki_sections if wiki_sections is not None else load_wiki_sections()
    for section in wiki_sections:
        if section.get('slug') == section_slug:
            return section
    return None


def find_wiki_page(section, page_path):
    if section is None:
        return None
    return _find_wiki_page(section.get('pages', []), _split_page_path(page_path), '')


def _find_wiki_page(pages, path_parts, parent_path):
    if not path_parts:
        return None

    requested_slug = path_parts[0]
    for page in pages:
        if page.get('slug') != requested_slug:
            continue

        page_path = _join_page_path(parent_path, page.get('slug'))
        page['path'] = page_path
        if len(path_parts) == 1:
            return page
        return _find_wiki_page(page.get('pages', []), path_parts[1:], page_path)
    return None


def load_wiki_page_content(page):
    """Load and render a wiki page Markdown file."""
    markdown_path = _resolve_markdown_path(page.get('file'))
    markdown_text = markdown_path.read_text(encoding='utf-8')
    return Markup(render_markdown(markdown_text))


def _resolve_markdown_path(page_file):
    if not page_file:
        raise FileNotFoundError('Wiki page is missing a Markdown file path.')

    markdown_path = (WIKI_PAGES_ROOT / page_file).resolve()
    pages_root = WIKI_PAGES_ROOT.resolve()
    if pages_root not in markdown_path.parents:
        raise FileNotFoundError('Wiki page path is outside the wiki pages directory.')
    if markdown_path.suffix.lower() != '.md':
        raise FileNotFoundError('Wiki page content must be a Markdown file.')
    return markdown_path


def hydrate_wiki_paths(wiki_sections):
    """Attach URL path fragments to each wiki page in the tree."""
    for section in wiki_sections:
        _hydrate_page_paths(section.get('pages', []), '')
    return wiki_sections


def _hydrate_page_paths(pages, parent_path):
    for page in pages:
        page_path = _join_page_path(parent_path, page.get('slug'))
        page['path'] = page_path
        _hydrate_page_paths(page.get('pages', []), page_path)


def _split_page_path(page_path):
    return [part for part in str(page_path or '').split('/') if part]


def _join_page_path(parent_path, slug):
    if parent_path:
        return f'{parent_path}/{slug}'
    return slug


def render_markdown(markdown_text):
    """Render the small Markdown subset used by wiki pages."""
    html_blocks = []
    paragraph_lines = []
    list_type = None
    blockquote_lines = []

    def flush_paragraph():
        if paragraph_lines:
            html_blocks.append(f'<p>{_render_inline(" ".join(paragraph_lines))}</p>')
            paragraph_lines.clear()

    def flush_list():
        nonlocal list_type
        if list_type:
            html_blocks.append(f'</{list_type}>')
            list_type = None

    def flush_blockquote():
        if blockquote_lines:
            text = ' '.join(blockquote_lines)
            html_blocks.append(f'<div class="wiki-note">{_render_inline(text)}</div>')
            blockquote_lines.clear()

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            flush_list()
            flush_blockquote()
            continue

        heading_match = re.match(r'^(#{1,4})\s+(.+)$', stripped)
        if heading_match:
            flush_paragraph()
            flush_list()
            flush_blockquote()
            level = min(len(heading_match.group(1)) + 1, 4)
            html_blocks.append(f'<h{level}>{_render_inline(heading_match.group(2))}</h{level}>')
            continue

        ordered_match = re.match(r'^\d+\.\s+(.+)$', stripped)
        unordered_match = re.match(r'^[-*]\s+(.+)$', stripped)
        if ordered_match or unordered_match:
            flush_paragraph()
            flush_blockquote()
            desired_list_type = 'ol' if ordered_match else 'ul'
            if list_type != desired_list_type:
                flush_list()
                list_type = desired_list_type
                html_blocks.append(f'<{list_type}>')
            item_text = ordered_match.group(1) if ordered_match else unordered_match.group(1)
            html_blocks.append(f'<li>{_render_inline(item_text)}</li>')
            continue

        if stripped.startswith('>'):
            flush_paragraph()
            flush_list()
            blockquote_lines.append(stripped[1:].strip())
            continue

        flush_list()
        flush_blockquote()
        paragraph_lines.append(stripped)

    flush_paragraph()
    flush_list()
    flush_blockquote()
    return '\n'.join(html_blocks)


def _render_inline(text):
    escaped = html.escape(text)
    escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)
    escaped = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', escaped)
    return escaped
