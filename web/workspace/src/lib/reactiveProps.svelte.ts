// SPDX-License-Identifier: MIT
/** Props a test can change after mounting, as a parent's state would. Tests only. */
export function reactiveProps<T extends object>(value: T): T {
  const props = $state(value);
  return props;
}
