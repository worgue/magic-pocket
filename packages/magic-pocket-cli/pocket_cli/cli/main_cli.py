import click

from pocket import __version__
from pocket_cli import django_cli
from pocket_cli.cli import (
    awscontainer_cli,
    cloudfront_cli,
    cloudfront_keys_cli,
    deploy_cli,
    destroy_cli,
    neon_cli,
    rds_cli,
    s3_cli,
    status_cli,
    tidb_cli,
    vpc_cli,
)


@click.group()
def main():
    pass


@main.command()
def version():
    """Print the version number."""
    click.echo(__version__)


main.add_command(deploy_cli.deploy)
main.add_command(destroy_cli.destroy)
main.add_command(status_cli.status)
main.add_command(django_cli.django)


@main.group()
def resource():
    pass


resource.add_command(vpc_cli.vpc)
resource.add_command(awscontainer_cli.awscontainer)
resource.add_command(neon_cli.neon)
resource.add_command(tidb_cli.tidb)
resource.add_command(rds_cli.rds)
resource.add_command(s3_cli.s3)
resource.add_command(cloudfront_cli.cloudfront)
resource.add_command(cloudfront_keys_cli.cloudfront_keys)
