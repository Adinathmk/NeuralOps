import nox

# Configure default sessions
nox.options.sessions = ["lint", "tests"]

@nox.session(python=False)
def lint(session):
    """Run linting checks (black, isort, flake8) on Django and FastAPI codebases."""
    session.log("Running black formatter check...")
    session.run("black", "--check", "django", "fastapi")
    
    session.log("Running isort import order check...")
    session.run("isort", "--check-only", "django", "fastapi")
    
    session.log("Running flake8 syntax and style check...")
    session.run("flake8", "django", "fastapi")

@nox.session(python=False)
def format(session):
    """Automatically format code and sort imports using black and isort."""
    session.log("Formatting code with black...")
    session.run("black", "django", "fastapi")
    
    session.log("Sorting imports with isort...")
    session.run("isort", "django", "fastapi")

@nox.session(python=False)
def tests(session):
    """Run the Django unit test suite with coverage reporting."""
    session.log("Navigating to Django directory and running test suite...")
    session.chdir("django")
    session.run("pytest")
