"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Only LoginView/LogoutView, not include("django.contrib.auth.urls") —
    # that module also routes password change and password reset, whose
    # POST handlers save passwords and send email. ADR 0020's read-only-UI
    # concern is that a non-staff Viewer shouldn't have to log in through a
    # page branded "Django administration"; two explicit routes satisfy
    # that while exposing strictly less surface (plan revision 2, decision
    # 10). The route names must stay "login"/"logout" — LOGIN_URL and
    # Django's own {% url 'login' %}/{% url 'logout' %} resolution depend
    # on them. LogoutView is POST-only in Django 6.0.7, so the nav's
    # sign-out is a one-button <form method="post"> — Django's own auth
    # view, not a write path of this app's (ADR 0020 decision 2's "no POST
    # handlers" is about the read-only UI's own views).
    path("accounts/login/", LoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("", include("inventory.urls")),
]
