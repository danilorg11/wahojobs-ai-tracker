import { createProductionGatewayHandler } from '../lib/gateway.mjs';

export default createProductionGatewayHandler({
  routeClass: 'jobs',
  ownsPath: (path) => path === '/jobs',
});
