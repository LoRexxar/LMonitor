#!/usr/bin/env python3
"""Standalone process entry point for the dedicated SimC worker."""

import os


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings')

    import django

    django.setup()

    from botend.simc_worker_entry import main as worker_main

    worker_main()


if __name__ == '__main__':
    main()
