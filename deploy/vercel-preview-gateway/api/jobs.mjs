import { createPreviewGatewayHandler } from '../lib/gateway.mjs';

export default createPreviewGatewayHandler({
  routeClass: 'jobs',
  ownsPath: (path) => path === '/jobs',
});
