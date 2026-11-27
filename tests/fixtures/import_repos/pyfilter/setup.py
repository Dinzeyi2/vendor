from setuptools import setup
setup(name="pyfilter", version="1.0", packages=["pyfilter"], description="Reverses each line of its input.",
      entry_points={"console_scripts": ["pyfilter = pyfilter.cli:main"]})
