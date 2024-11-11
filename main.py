#!/usr/bin/env python3

import re
import sys

def extract_repo(url):
    """Extract the GitHub repository name from the URL."""
    match = re.match(r'https://github\.com/([^/]+/[^/]+)', url)
    return match.group(1) if match else ''

def add_badges(lines):
    """Add GitHub badges to lines containing GitHub repository URLs."""
    processed_lines = []
    for line in lines:
        if 'https://github.com/' in line:
            # Extract the GitHub URL
            url_match = re.search(r'https://github\.com/\S+', line)
            if url_match:
                repo_url = url_match.group(0)
                repo_name = extract_repo(repo_url)
                if repo_name:
                    processed_lines.append(line)
                    processed_lines.append('')  # Add a blank line
                    # Add shields.io badges
                    badges = [
                        f'  ![Last Commit](https://img.shields.io/github/last-commit/{repo_name})',
                        f'![License](https://img.shields.io/github/license/{repo_name})',
                        f'![Issues](https://img.shields.io/github/issues/{repo_name})',
                        f'![Stars](https://img.shields.io/github/stars/{repo_name})',
                        f'![Forks](https://img.shields.io/github/forks/{repo_name})',
                        ''
                    ]
                    processed_lines.extend(badges)
                else:
                    processed_lines.append(line)
            else:
                processed_lines.append(line)
        else:
            processed_lines.append(line)
    return processed_lines

def style_badges(lines):
    """Add the 'flat-square' style to all image badges in the lines."""
    def replace_style(match):
        alt_text = match.group(1)
        url = match.group(2)
        if '?' in url:
            url += '&style=flat-square'
        else:
            url += '?style=flat-square'
        return f'![{alt_text}]({url})'

    styled_lines = []
    pattern = re.compile(r'!\[([^\]]+)\]\(([^)]+)\)')
    for line in lines:
        styled_line = pattern.sub(replace_style, line)
        styled_lines.append(styled_line)
    return styled_lines

def remove_extra_parentheses(lines):
    """Remove extra closing parentheses at the end of image markdown links."""
    corrected_lines = []
    for line in lines:
        # Match image links with potential extra closing parentheses
        corrected_line = re.sub(r'(!\[[^\]]*\]\([^\)]*\))\)+', r'\1', line)
        corrected_lines.append(corrected_line)
    return corrected_lines

def main():
    if len(sys.argv) != 2:
        print("Usage: python script.py <input_markdown_file>")
        sys.exit(1)

    input_file = sys.argv[1]

    with open(input_file, 'r', encoding='utf-8') as f:
        lines = [line.rstrip('\n') for line in f]

    lines_with_badges = add_badges(lines)

    lines_with_square_badges = style_badges(lines_with_badges)

    final_lines = remove_extra_parentheses(lines_with_square_badges)

    for line in final_lines:
        print(line)

main()
