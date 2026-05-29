import nox

# Configure default sessions
nox.options.sessions = ["lint", "tests"]


@nox.session(python=False)
def lint(session):
    """Run linting checks (black, isort, flake8) on FastAPI codebase."""
    session.log("Running black formatter check...")
    session.run("black", "--check", ".")

    session.log("Running isort import order check...")
    session.run("isort", "--check-only", ".")

    session.log("Running flake8 syntax and style check...")
    session.run("flake8", ".")


@nox.session(python=False)
def format(session):
    """Automatically format code and sort imports using black and isort."""
    session.log("Formatting code with black...")
    session.run("black", ".")

    session.log("Sorting imports with isort...")
    session.run("isort", ".")


@nox.session(python=False)
def tests(session):
    """Run the FastAPI unit test suite."""
    session.log("Running FastAPI test suite via pytest...")
    session.run("pytest", "tests/")
