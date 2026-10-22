from django.core.management.base import BaseCommand, CommandError

from care_emergency.app_server import setup_app_server


class Command(BaseCommand):
    help = (
        "Creates the patient app's service account (poh-app-server) and its "
        "roles, and gives it the app-server role at every facility. Safe to run "
        "again. Then create its API key in Settings → API keys."
    )

    def handle(self, *args, **options):
        try:
            user, granted = setup_app_server()
        except RuntimeError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(
            self.style.SUCCESS(
                f"{user.username} is ready; added to {granted} more facilities."
            )
        )
