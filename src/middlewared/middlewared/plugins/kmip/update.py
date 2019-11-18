import middlewared.sqlalchemy as sa

from middlewared.service import accepts, ConfigService, private, ValidationErrors
from middlewared.schema import Bool, Dict, Int, Str
from middlewared.validators import Port


class KMIPModel(sa.Model):
    __tablename__ = 'system_kmip'

    id = sa.Column(sa.Integer(), primary_key=True)
    server = sa.Column(sa.String(128), default=None, nullable=True)
    port = sa.Column(sa.SmallInteger(), default=5696)
    certificate_id = sa.Column(sa.ForeignKey('system_certificate.id'), index=True, nullable=True)
    certificate_authority_id = sa.Column(sa.ForeignKey('system_certificateauthority.id'), index=True, nullable=True)
    manage_sed_disks = sa.Column(sa.Boolean(), default=False)
    manage_zfs_keys = sa.Column(sa.Boolean(), default=False)
    enabled = sa.Column(sa.Boolean(), default=False)


class KMIPService(ConfigService):
    class Config:
        datastore = 'system_kmip'
        datastore_extend = 'kmip.kmip_extend'

    @private
    async def kmip_extend(self, data):
        for k in filter(lambda v: data[v], ('certificate', 'certificate_authority')):
            data[k] = data[k]['id']
        return data

    @accepts(
        Dict(
            'kmip_update',
            Bool('enabled'),
            Bool('force_clear'),
            Bool('manage_sed_disks'),
            Bool('manage_zfs_keys'),
            Bool('validate'),
            Int('certificate', null=True),
            Int('certificate_authority', null=True),
            Int('port', validators=[Port()]),
            Str('server'),
            update=True
        )
    )
    async def do_update(self, data):
        old = await self.config()
        new = old.copy()
        new.update(data)
        verrors = ValidationErrors()

        if not new['server'] and new['enabled']:
            verrors.add('kmip_update.server', 'Please specify a valid hostname or an IPv4 address')

        if new['enabled']:
            verrors.extend((await self.middleware.call(
                'certificate.cert_services_validation', new['certificate'], 'kmip_update.certificate', False
            )))

        ca = await self.middleware.call('certificateauthority.query', [['id', '=', new['certificate_authority']]])
        if ca and not verrors:
            ca = ca[0]
            if not await self.middleware.call(
                'cryptokey.validate_cert_with_chain',
                (await self.middleware.call('certificate._get_instance', new['certificate']))['certificate'],
                [ca['certificate']]
            ):
                verrors.add(
                    'kmip_update.certificate_authority',
                    'Certificate chain could not be verified with specified certificate authority.'
                )
        elif not ca and new['enabled']:
            verrors.add('kmip_update.certificate_authority', 'Please specify a valid id.')

        if new.pop('validate', True) and new['enabled'] and not verrors:
            if not await self.middleware.call('kmip.test_connection', new):
                verrors.add('kmip_update.server', f'Unable to connect to {new["server"]} KMIP server.')

        force_clear = new.pop('force_clear', False)
        clear_keys = False
        sync_error = 'KMIP sync is pending, please make sure database and KMIP server ' \
                     'are in sync before proceeding with this operation.'
        if old['enabled'] != new['enabled'] and await self.middleware.call('kmip.kmip_sync_pending'):
            if force_clear:
                clear_keys = True
            else:
                verrors.add('kmip_update.enabled', sync_error)

        verrors.check()

        if clear_keys:
            await self.middleware.call('kmip.clear_sync_pending_keys')

        await self.middleware.call(
            'datastore.update', self._config.datastore, old['id'], new,
        )

        await self.middleware.call('service.start', 'kmip')
        if new['enabled'] and old['enabled'] != new['enabled']:
            await self.middleware.call('kmip.initialize_keys')
        if any(old[k] != new[k] for k in ('enabled', 'manage_zfs_keys', 'manage_sed_keys')):
            await self.middleware.call('kmip.sync_keys')

        return await self.config()
