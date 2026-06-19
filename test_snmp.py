import asyncio, pysnmp.hlapi.v3arch.asyncio as hlapi
async def test():
    engine = hlapi.SnmpEngine()
    transport = await hlapi.UdpTransportTarget.create(('10.160.4.1', 161), timeout=2, retries=1)
    res = await hlapi.get_cmd(engine, hlapi.CommunityData('BynetSec-RO'), transport, hlapi.ContextData(), hlapi.ObjectType(hlapi.ObjectIdentity('1.3.6.1.2.1.1.5.0')))
    errIndication, errStatus, errIndex, varBinds = res
    print(errIndication or 'Success')
asyncio.run(test())
