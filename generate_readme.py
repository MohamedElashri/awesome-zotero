#!/usr/bin/env python3

import re
import sys


def extract_repo(url):
    """Extract the GitHub repository name from the URL."""
    match = re.match(r'https://github\.com/([^/]+/[^/]+)', url)
    return match[1] if match else ''


def add_badges(lines):
    """Add GitHub badges to lines containing GitHub repository URLs."""
    processed_lines = []
    for line in lines:
        if 'https://github.com/' in line:
            if url_match := re.search(r'https://github\.com/\S+', line):
                repo_url = url_match[0]
                repo_name = extract_repo(repo_url)
                processed_lines.append(line)
                if repo_name:
                    processed_lines.append('')  # Add a blank line
                    # Add shields.io badges
                    badges = [
                        f'  ![Last Commit](https://img.shields.io/github/last-commit/{repo_name}?style=flat-square)',
                        f'![License](https://img.shields.io/github/license/{repo_name}?style=flat-square)',
                        f'![Issues](https://img.shields.io/github/issues/{repo_name}?style=flat-square)',
                        f'![Stars](https://img.shields.io/github/stars/{repo_name}?style=flat-square)',
                        f'![Forks](https://img.shields.io/github/forks/{repo_name}?style=flat-square)',
                        ''
                    ]
                    processed_lines.extend(badges)
            else:
                processed_lines.append(line)
        else:
            processed_lines.append(line)
    return processed_lines


def ensure_punctuation(lines):
    """Ensure all list item descriptions end with a period."""
    punctuated_lines = []
    for line in lines:
        # Match lines that start with list items and have descriptions
        if re.match(r'- \[.*?\]\(.*?\) - .*[^.]$', line):
            line += '.'  # Append a period
        punctuated_lines.append(line)
    return punctuated_lines


def remove_extra_parentheses(lines):
    """Remove extra closing parentheses at the end of image markdown links."""
    corrected_lines = []
    for line in lines:
        corrected_line = re.sub(r'(!\[[^\]]*\]\([^\)]*\))\)+', r'\1', line)
        corrected_lines.append(corrected_line)
    return corrected_lines


def add_awesome_badge(lines):
    """Ensure the Awesome badge is correctly added."""
    badge = '[![Awesome](https://awesome.re/badge-flat.svg)](https://awesome.re)'
    if lines and badge not in lines[0]:
        lines.insert(0, badge)
    return lines


def main():
    if len(sys.argv) != 2:
        print("Usage: python script.py <input_markdown_file>")
        sys.exit(1)

    input_file = sys.argv[1]

    with open(input_file, 'r', encoding='utf-8') as f:
        lines = [line.rstrip('\n') for line in f]

    lines = add_awesome_badge(lines)
    lines = add_badges(lines)
    lines = ensure_punctuation(lines)
    lines = remove_extra_parentheses(lines)

    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
