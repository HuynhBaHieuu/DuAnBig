import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tracon.settings")
application = get_wsgi_application()
app = application  # Vercel (@vercel/python) tìm biến 'app'
