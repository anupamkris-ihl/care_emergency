from django.core.management.base import BaseCommand, CommandError

from care_emergency.pincodes import (
    DirectoryError,
    api_key,
    replace_directory,
    rows_from_api,
    rows_from_csv,
)


class Command(BaseCommand):
    help = (
        "Loads India Post's PIN directory, replacing the current one: from "
        "data.gov.in when DATA_GOV_IN_API_KEY is set, or from the directory's "
        "CSV download with --file. A failed load keeps the old directory."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            help='CSV of the "All India Pincode Directory" from data.gov.in.',
        )

    def handle(self, *args, **options):
        if options["file"]:
            records = rows_from_csv(options["file"])
        elif key := api_key():
            records = rows_from_api(key)
        else:
            msg = "Set DATA_GOV_IN_API_KEY, or pass --file with the directory CSV"
            raise CommandError(msg)
        try:
            count = replace_directory(records)
        except (DirectoryError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"Loaded {count} post offices"))
