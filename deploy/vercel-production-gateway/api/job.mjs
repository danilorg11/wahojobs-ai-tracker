import { createProductionGatewayHandler } from '../lib/gateway.mjs';
import { publishedPathSet } from '../lib/release.mjs';

export default createProductionGatewayHandler({
  routeClass: 'detail',
  ownsPath: (path) => publishedPathSet.has(path),
});
