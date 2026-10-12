import { Compass } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '@/components/common/status'
import { Button } from '@/components/ui/button'

export function NotFoundPage() {
  return (
    <EmptyState
      icon={Compass}
      title="Page not found"
      action={
        <Button asChild variant="outline">
          <Link to="/">Go home</Link>
        </Button>
      }
    >
      That page does not exist in this workspace.
    </EmptyState>
  )
}
