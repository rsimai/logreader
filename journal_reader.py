#!/usr/bin/env python3

import os
import sys
from functools import partial
import argparse

try:
    from systemd import journal
except ImportError:
    print("Error: The 'python-systemd' library is not installed.", file=sys.stderr)
    print("Please install it using your package manager (e.g., 'sudo apt-get install python3-systemd')", file=sys.stderr)
    print("or via pip ('pip install python-systemd').", file=sys.stderr)
    sys.exit(1)
try:
    import requests
except ImportError:
    print("Error: The 'requests' library is not installed.", file=sys.stderr)
    print("Please install it via pip ('pip install requests').", file=sys.stderr)
    sys.exit(1)

def process_entry(entry, llama_url, output_file, prompt_template, model_name):
    """
    Sends a journal entry to a Llama service and writes the response to a file.
    """
    message = entry.get('MESSAGE', '')
    if not message or not isinstance(message, str):
        # Skip empty or non-string messages (e.g., binary data)
        return

    prompt = prompt_template.format(message=message)
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
    }

    try:
        response = requests.post(llama_url, json=payload, timeout=30)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

        response_data = response.json()
        content = response_data.get("response", "").strip()

        if content:
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(f"--- Log Entry ---\n{message}\n")
                f.write(f"--- Llama Analysis ---\n{content}\n\n")
            print(f"INFO: Analysis for entry saved to {output_file}")
        else:
            print("WARNING: Llama service returned an empty response.", file=sys.stderr)

    except requests.exceptions.RequestException as e:
        print(f"ERROR: Could not contact Llama service at {llama_url}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during processing: {e}", file=sys.stderr)

def main():
    """Main function to set up and run the journal reader."""
    parser = argparse.ArgumentParser(
        description="Read from the systemd journal, analyze entries with a Llama service, and save the output.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        '-j', '--journal-type',
        choices=['system', 'user'],
        default=None,
        help="""Type of journal to read.
'system': Read the system-wide journal (requires root or 'adm' group).
'user':   Read the current user's journal.
Default: 'system' if running as root, 'user' otherwise."""
    )
    parser.add_argument(
        '-o', '--output-file',
        type=str,
        required=True,
        help='Path to the file to save Llama service responses.'
    )
    parser.add_argument(
        '--llama-url',
        type=str,
        default='http://localhost:11434/api/generate',
        help='URL of the Llama service API endpoint. (default: http://localhost:11434/api/generate for Ollama)'
    )
    parser.add_argument(
        '-m', '--model',
        type=str,
        default=None,
        help='The name of the model to use (e.g., "llama3").\n'
             'If not provided, the script will auto-detect and use the first available model.'
    )
    parser.add_argument(
        '--prompt-template',
        type=str,
        default="Analyze the following system log entry and explain its meaning and significance. "
                "Log Entry: {message}",
        help='A template for the prompt sent to the Llama service. Must include "{message}".'
    )

    args = parser.parse_args()
    journal_type_arg = args.journal_type
    model_name = args.model

    # If no model is specified, auto-detect one from the Ollama service
    if not model_name:
        print("INFO: No model specified. Attempting to auto-detect an available model...")
        # Derive the tags URL from the base generate URL (e.g., http://.../api/generate -> http://.../api/tags)
        tags_url = args.llama_url.replace('/api/generate', '/api/tags')
        try:
            response = requests.get(tags_url, timeout=10)
            response.raise_for_status()
            models_data = response.json()

            if not models_data.get('models'):
                print("ERROR: Could not find any available models from the Llama service.", file=sys.stderr)
                print("ERROR: Please pull a model first (e.g., 'ollama pull llama3') or specify one with --model.", file=sys.stderr)
                sys.exit(1)

            model_name = models_data['models'][0]['name']
            print(f"INFO: Auto-selected model: '{model_name}'")

        except requests.exceptions.RequestException as e:
            print(f"ERROR: Failed to query available models from {tags_url}: {e}", file=sys.stderr)
            sys.exit(1)

    is_root = os.geteuid() == 0

    if journal_type_arg is None:
        journal_type = 'system' if is_root else 'user'
    else:
        journal_type = journal_type_arg

    print(f"Starting journal reader...")
    print(f"Effective User ID: {os.geteuid()}")
    print(f"Journal type:      {journal_type}")
    print(f"Using model:       {model_name}")
    print("Tailing journal in real-time... (Press Ctrl+C to exit)")

    try:
        try:
            # Modern, preferred method using flags (for newer python-systemd versions)
            if journal_type == 'user':
                reader = journal.Reader(flags=journal.JOURNAL_CURRENT_USER)
            else:  # 'system'
                if not is_root:
                    print("Warning: Reading the system journal typically requires root privileges.", file=sys.stderr)
                reader = journal.Reader(flags=journal.JOURNAL_SYSTEM)
            print("Info: Using modern 'flags' to open journal.")

        except AttributeError:
            # Fallback for very old python-systemd versions (e.g., on CentOS 7)
            # that lack the 'flags' argument and constants.
            print("Warning: Using a legacy version of python-systemd. The '--journal-type' argument will be ignored.", file=sys.stderr)
            print("Info: The journal type will be determined by user privileges (user for non-root, system for root).", file=sys.stderr)

            # In old versions, Reader() with no args does the right thing based on EUID.
            # The user's choice via --journal-type cannot be honored.
            reader = journal.Reader()

        # Seek to the end to only get new messages
        reader.seek_tail()
        # The first entry after seek_tail() is the last old one, so we skip it.
        reader.get_previous()

    except FileNotFoundError:
        print(f"Error: Could not open journal. Is systemd-journald running?", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied to read the {journal_type} journal.", file=sys.stderr)
        if journal_type == 'system':
            print("Try running with 'sudo' for the system journal.", file=sys.stderr)
        sys.exit(1)

    # Create a partial function to handle processing, pre-filling the config arguments.
    processing_function = partial(
        process_entry,
        llama_url=args.llama_url,
        output_file=args.output_file,
        prompt_template=args.prompt_template,
        model_name=model_name
    )

    try:
        while True:
            # wait() blocks until the journal changes.
            if reader.wait():
                # Iterate over new entries that have arrived
                for entry in reader:
                    processing_function(entry)

    except KeyboardInterrupt:
        print("\nInterrupted by user. Terminating immediately.")
        # Exit gracefully. The standard exit code for a script
        # interrupted by Ctrl+C is 130.
        sys.exit(130)

if __name__ == "__main__":
    main()